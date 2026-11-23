"""`gridcast repair ieso-rows` against real Postgres: stale IESO rows are rewritten as of the
cutoff from the stored snapshots; hours without an IESO run row are never created."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pandas as pd
import pytest
from sqlalchemy import Connection, Engine, text

from gridcast import db
from gridcast.jobs import predict, repair

pytestmark = pytest.mark.integration

ISSUE = datetime(2026, 10, 30, 12, tzinfo=UTC)  # cutoff 12:30Z
OLD = ISSUE - timedelta(days=2)  # the version the ingest had read in time
NEW = ISSUE - timedelta(hours=1)  # created before the cutoff, read too late


@pytest.fixture(scope="module")
def engine(fresh_db_url: str) -> Engine:
    db.upgrade(fresh_db_url)
    eng = db.make_engine(fresh_db_url)
    with eng.begin() as conn:
        for created, mw, version in ((OLD, 14000.0, 1), (ISSUE + timedelta(hours=2), 1.0, 3)):
            _snapshot(conn, created, mw, version)
    # The hourly job wrote the IESO rows from the only version it had read (v1), minus h=24.
    rows = predict.ieso_rows(eng, ISSUE)
    predict.write_predictions(eng, rows[rows["horizon"] < 24], uuid.uuid4(), "scheduled")
    with eng.begin() as conn:  # IESO then read v2 late; it was created before the cutoff
        _snapshot(conn, NEW, 15000.0, 2)
        for h in range(1, 25):
            conn.execute(
                text(
                    "INSERT INTO actuals (target_time, ontario_demand_mw, available_at,"
                    " available_at_kind) VALUES (:t, 15000, :t, 'observed')"
                ),
                {"t": ISSUE + timedelta(hours=h)},
            )
    return eng


def _snapshot(conn: Connection, created: datetime, mw: float, version: int) -> None:
    for h in range(1, 31):
        t = ISSUE + timedelta(hours=h)
        conn.execute(
            text(
                "INSERT INTO ieso_forecast_snapshots (created_at, delivery_date, target_time,"
                " energy_mw, version, source_file) VALUES (:c, :d, :t, :mw, :v, 'f.xml')"
            ),
            {"c": created, "d": (t - timedelta(hours=5)).date(), "t": t, "mw": mw, "v": version},
        )


def _ieso(engine: Engine) -> pd.DataFrame:
    with engine.connect() as conn:
        return pd.read_sql(
            text(
                "SELECT issue_time, horizon, p50::float8 AS p50, run_kind FROM v_predictions"
                " WHERE model_name = 'ieso_adequacy' ORDER BY issue_time, horizon"
            ),
            conn,
        )


def test_dry_run_counts_and_writes_nothing(engine: Engine) -> None:
    out = repair.ieso_rows(engine, ISSUE - timedelta(hours=3), ISSUE + timedelta(hours=3))
    assert out["issue_hours"] == 1  # only the hour with an IESO run row
    assert (out["rows_changed"], out["rows_new"], out["rows_unchanged"]) == (23, 1, 0)
    assert out["rows_written"] == 0 and "status" not in out
    assert out["ieso_mape_stored"] == {"mape": pytest.approx(6.667, abs=1e-3), "n": 23}
    assert out["ieso_mape_recomputed"] == {"mape": 0.0, "n": 24}
    ieso = _ieso(engine)
    assert len(ieso) == 23 and (ieso["p50"] == 14000).all()


def test_apply_rewrites_the_rows_as_of_the_cutoff(engine: Engine) -> None:
    out = repair.ieso_rows(
        engine, ISSUE - timedelta(hours=3), ISSUE + timedelta(hours=3), apply=True
    )
    assert out["status"] == "ok" and out["rows_written"] == 24
    ieso = _ieso(engine)
    assert len(ieso) == 24
    assert (ieso["p50"] == 15000).all()  # v2 (created 11:00Z <= 12:30Z), never v3 (14:00Z)
    assert set(ieso["run_kind"]) == {"manual"}
    again = repair.ieso_rows(engine, ISSUE, ISSUE, apply=True)
    assert (again["rows_unchanged"], again["rows_changed"], again["rows_new"]) == (24, 0, 0)
    assert again["rows_written"] == 0
    with engine.connect() as conn:
        assert conn.execute(text("SELECT count(*) FROM prediction_runs")).scalar_one() == 1
