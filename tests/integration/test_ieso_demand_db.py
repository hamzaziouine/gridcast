"""C2 against real Postgres: append-only versions, idempotent re-runs, actuals revisions."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlalchemy import Engine, text

from gridcast import db
from gridcast.ingest import ieso_demand as d
from gridcast.ingest.http import Fetched

pytestmark = pytest.mark.integration

FIXTURE = (Path(__file__).parents[1] / "fixtures" / "ieso_pub_demand_sample.csv").read_text()
NAME = "PUB_Demand_2026_v295.csv"


def _fetched(body: str, name: str, fetched_at: datetime) -> Fetched:
    return Fetched(d.BASE_URL + name, 200, body, fetched_at, None)


@pytest.fixture(scope="module")
def engine(fresh_db_url: str) -> Engine:
    db.upgrade(fresh_db_url)
    return db.make_engine(fresh_db_url)


def _snapshot(engine: Engine) -> list[tuple[object, ...]]:
    with engine.connect() as conn:
        return list(
            conn.execute(
                text(
                    "SELECT target_time, file_name, available_at, available_at_kind"
                    " FROM raw_demand_history ORDER BY 1, 2"
                )
            )
        )


def test_backfill_twice_changes_nothing(engine: Engine) -> None:
    t0 = datetime(2026, 10, 23, 20, 0, tzinfo=UTC)
    first = d.ingest_pub_demand(engine, _fetched(FIXTURE, NAME, t0), "modelled")
    assert (first.rows_parsed, first.rows_inserted, first.actuals_changed) == (145, 144, 144)
    before = _snapshot(engine)
    again = d.ingest_pub_demand(
        engine, _fetched(FIXTURE, NAME, datetime(2026, 10, 23, 21, 0, tzinfo=UTC)), "modelled"
    )
    assert (again.rows_inserted, again.actuals_changed) == (0, 0)
    assert _snapshot(engine) == before  # rows and every available_at unchanged
    with engine.connect() as conn:
        n, kinds, rev = conn.execute(
            text(
                "SELECT count(*), array_agg(DISTINCT available_at_kind), max(revision) FROM actuals"
            )
        ).one()
    assert (n, kinds, rev) == (144, ["modelled"], 1)


def test_revision_appends_a_row_and_bumps_actuals(engine: Engine) -> None:
    # Next day's file: 2026-10-22 HE24 revised (+10 MW), and 2026-10-23 HE1 now published.
    revised = FIXTURE.replace("2026-10-22,24,15840,14161", "2026-10-22,24,15850,14171")
    t1 = datetime(2026, 10, 24, 13, 20, tzinfo=UTC)
    res = d.ingest_pub_demand(engine, _fetched(revised, "PUB_Demand_2026_v296.csv", t1), "modelled")
    assert res.rows_inserted == 2  # the revision + the newly published HE1
    assert res.actuals_changed == 2
    with engine.connect() as conn:
        rows = conn.execute(
            text(
                "SELECT ontario_demand_mw, available_at, available_at_kind, file_version"
                " FROM raw_demand_history WHERE target_time = '2026-10-23 04:00Z'"
                " ORDER BY available_at"
            )
        ).all()
        actual = conn.execute(
            text(
                "SELECT ontario_demand_mw, revision, available_at_kind FROM actuals"
                " WHERE target_time = '2026-10-23 04:00Z'"
            )
        ).one()
        he1 = conn.execute(
            text(
                "SELECT available_at, available_at_kind FROM raw_demand_history"
                " WHERE target_time = '2026-10-23 05:00Z'"
            )
        ).one()
    # The first version keeps its modelled stamp; the revision is observed at its fetch time.
    assert [(float(r[0]), r[1], r[2], r[3]) for r in rows] == [
        (14161.0, datetime(2026, 10, 23, 13, 0, tzinfo=UTC), "modelled", "v295"),
        (14171.0, t1, "observed", "v296"),
    ]
    assert (float(actual[0]), actual[1], actual[2]) == (14171.0, 2, "observed")
    assert he1 == (datetime(2026, 10, 24, 13, 0, tzinfo=UTC), "modelled")


def test_observed_mode_and_view(engine: Engine) -> None:
    t2 = datetime(2026, 10, 25, 13, 20, tzinfo=UTC)
    res = d.ingest_pub_demand(engine, _fetched(FIXTURE, "PUB_Demand.csv", t2), "observed")
    # Reverting HE24 to the v295 value is a change against the latest version (v296).
    assert res.rows_inserted == 1
    with engine.connect() as conn:
        latest = conn.execute(
            text(
                "SELECT ontario_demand_mw, file_name FROM raw_demand"
                " WHERE target_time = '2026-10-23 04:00Z' AND source = 'pub_demand'"
            )
        ).one()
        rev = conn.execute(
            text("SELECT revision FROM actuals WHERE target_time = '2026-10-23 04:00Z'")
        ).scalar_one()
    assert (float(latest[0]), latest[1]) == (14161.0, "PUB_Demand.csv")
    assert rev == 3


def test_contract_failure_writes_nothing(engine: Engine) -> None:
    import pandera.errors

    # Market Demand 17 MW instead of 17,265: a unit or parsing error the contract must stop.
    broken = FIXTURE.replace("2026-10-17,1,17265,13989", "2026-10-17,1,17.265,13989")
    with engine.connect() as conn:
        before = conn.execute(text("SELECT count(*) FROM raw_demand_history")).scalar_one()
    t = datetime(2026, 10, 26, 13, 20, tzinfo=UTC)
    with pytest.raises((pandera.errors.SchemaError, pandera.errors.SchemaErrors)):
        d.ingest_pub_demand(engine, _fetched(broken, "PUB_Demand_2026_v298.csv", t), "observed")
    with engine.connect() as conn:
        after = conn.execute(text("SELECT count(*) FROM raw_demand_history")).scalar_one()
    assert after == before
