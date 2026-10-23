"""C4 against real Postgres: RealtimeTotals and Adequacy3 ingest with fake fetchers."""

from __future__ import annotations

import gzip
from datetime import UTC, date, datetime
from pathlib import Path

import pytest
from sqlalchemy import Engine, text

from gridcast import db
from gridcast.ingest import ieso_adequacy as a
from gridcast.ingest import ieso_realtime as r
from gridcast.ingest.http import Fetched

pytestmark = pytest.mark.integration

FIXTURES = Path(__file__).parents[1] / "fixtures"
NOW = datetime(2026, 10, 23, 20, 30, tzinfo=UTC)


@pytest.fixture(scope="module")
def engine(fresh_db_url: str) -> Engine:
    db.upgrade(fresh_db_url)
    return db.make_engine(fresh_db_url)


def test_realtime_ingest_recent(engine: Engine) -> None:
    complete = (FIXTURES / "ieso_realtime_sample.csv").read_text()
    partial = (FIXTURES / "ieso_realtime_partial_sample.csv").read_text()
    requested: list[str] = []

    def fake(url: str) -> Fetched:
        name = url.rsplit("/", 1)[-1]
        requested.append(name)
        body = {"PUB_RealtimeTotals_2026102314.csv": complete}.get(name)
        if name == "PUB_RealtimeTotals_2026102311.csv":
            body = partial
        return Fetched(url, 200 if body else 404, body or "", NOW, None)

    counts = r.ingest_recent(engine, fake, NOW, lookback_h=6, lag_h=1)
    # Due hours: target 14:00Z .. 19:00Z = HE10 .. HE15 of 2026-10-23.
    assert requested[0] == "PUB_RealtimeTotals_2026102310.csv"
    assert requested[-1] == "PUB_RealtimeTotals_2026102315.csv"
    assert counts == {
        "due": 6,
        "stored_before": 0,
        "fetched": 2,
        "inserted": 1,
        "missing": 4,
        "partial": 1,
    }
    with engine.connect() as conn:
        row = conn.execute(
            text(
                "SELECT target_time, market_demand_mw, ontario_demand_mw, n_intervals,"
                " available_at_kind, available_at FROM raw_demand_history"
                " WHERE source = 'realtime_totals'"
            )
        ).one()
    assert row[0] == datetime(2026, 10, 23, 18, tzinfo=UTC)
    assert (float(row[1]), row[2], row[3], row[4], row[5]) == (16170.21, None, 12, "observed", NOW)
    again = r.ingest_recent(engine, fake, NOW, lookback_h=6, lag_h=1)
    assert (again["stored_before"], again["inserted"]) == (1, 0)


def test_adequacy_ingest_recent(engine: Engine, tmp_path: Path) -> None:
    xml = (FIXTURES / "ieso_adequacy_sample.xml").read_text()
    listing = (
        '<a href="PUB_Adequacy3_20261024_v68.xml">x</a>'
        '<a href="PUB_Adequacy3_20261024.xml">x</a>'
        '<a href="PUB_Adequacy3_20261127_v1.xml">x</a>'
    )

    def fake(url: str) -> Fetched:
        body = listing if url == a.BASE_URL else xml
        return Fetched(url, 200, body, NOW, None)

    counts = a.ingest_recent(engine, fake, date(2026, 10, 23), archive_root=tmp_path)
    assert counts == {"listed": 3, "pending": 1, "files": 1, "rows": 24}
    archived = tmp_path / "ieso/adequacy3/2026/10/24/PUB_Adequacy3_20261024_v68.xml.gz"
    assert gzip.decompress(archived.read_bytes()).decode() == xml
    with engine.connect() as conn:
        n, created, version, src = conn.execute(
            text(
                "SELECT count(*), min(created_at), min(version), min(source_file)"
                " FROM ieso_forecast_snapshots"
            )
        ).one()
    assert (n, created, version, src) == (
        24,
        datetime(2026, 10, 23, 17, 53, 16, tzinfo=UTC),
        68,
        "PUB_Adequacy3_20261024_v68.xml",
    )
    again = a.ingest_recent(engine, fake, date(2026, 10, 23), archive_root=tmp_path)
    assert (again["pending"], again["rows"]) == (0, 0)
