"""C4 against real Postgres: RealtimeTotals and Adequacy3 ingest with fake fetchers."""

from __future__ import annotations

import gzip
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import Connection, Engine, text

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
    assert counts == {"listed": 3, "pending": 1, "files": 1, "rows": 24, "pruned": 0}
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


def _snapshot_rows(conn: Connection, d: date, created: datetime, version: int) -> None:
    for he in range(1, 25):
        conn.execute(
            text(
                "INSERT INTO ieso_forecast_snapshots (created_at, delivery_date, target_time,"
                " energy_mw, version, source_file, fetched_at) VALUES (:c, :d, :t, 15000, :v,"
                " :f, :c)"
            ),
            {
                "c": created,
                "d": d,
                "t": datetime(d.year, d.month, d.day, tzinfo=UTC) + timedelta(hours=4 + he),
                "v": version,
                "f": f"PUB_Adequacy3_{d:%Y%m%d}_v{version}.xml",
            },
        )
        conn.execute(
            text(
                "INSERT INTO ieso_adequacy_files (source_file, delivery_date, version, created_at,"
                " fetched_at) VALUES (:f, :d, :v, :c, :c) ON CONFLICT DO NOTHING"
            ),
            {"f": f"PUB_Adequacy3_{d:%Y%m%d}_v{version}.xml", "d": d, "v": version, "c": created},
        )


def test_prune_keeps_recent_versions_and_newest_older_one(engine: Engine) -> None:
    d = date(2026, 12, 10)
    cutoff = datetime(2026, 12, 8, 5, tzinfo=UTC)  # (D - 2) 00:00 EST
    created = [
        cutoff - timedelta(days=20),
        cutoff - timedelta(days=3),
        cutoff - timedelta(minutes=1),  # newest before the cutoff: kept
        cutoff,  # on the cutoff: kept
        cutoff + timedelta(hours=30),
    ]
    with engine.begin() as conn:
        for v, c in enumerate(created, start=1):
            _snapshot_rows(conn, d, c, v)
    out = a.prune_snapshots(engine)
    assert out == {"files_pruned": 2, "rows_deleted": 48}
    with engine.connect() as conn:
        kept = (
            conn.execute(
                text(
                    "SELECT DISTINCT version FROM ieso_forecast_snapshots WHERE delivery_date = :d"
                    " ORDER BY 1"
                ),
                {"d": d},
            )
            .scalars()
            .all()
        )
        flags = dict(
            conn.execute(
                text("SELECT version, rows_kept FROM ieso_adequacy_files WHERE delivery_date = :d"),
                {"d": d},
            ).all()
        )
    assert kept == [3, 4, 5]
    assert flags == {1: False, 2: False, 3: True, 4: True, 5: True}
    assert a.prune_snapshots(engine) == {"files_pruned": 0, "rows_deleted": 0}  # idempotent


def test_pruned_versions_are_not_downloaded_again(engine: Engine) -> None:
    d = date(2026, 12, 10)  # versions 1-2 were pruned by the test above
    listing = "".join(f'<a href="PUB_Adequacy3_{d:%Y%m%d}_v{v}.xml">x</a>' for v in (1, 2, 3))
    requested: list[str] = []

    def fake(url: str) -> Fetched:
        requested.append(url)
        return Fetched(url, 200, listing, NOW, None)

    counts = a.ingest_recent(engine, fake, d, days_ahead=0, archive_root=None)
    assert counts["pending"] == 0 and requested == [a.BASE_URL]


def test_adequacy_archive_to_object_store(engine: Engine) -> None:
    from moto import mock_aws

    from gridcast.storage.objstore import ObjstoreSettings, make_client, private_store

    xml = (FIXTURES / "ieso_adequacy_sample.xml").read_text()
    with engine.begin() as conn:
        conn.execute(text("DELETE FROM ieso_forecast_snapshots WHERE delivery_date = '2026-10-24'"))
        conn.execute(text("DELETE FROM ieso_adequacy_files WHERE delivery_date = '2026-10-24'"))

    def fake(url: str) -> Fetched:
        body = '<a href="PUB_Adequacy3_20261024_v68.xml">x</a>' if url == a.BASE_URL else xml
        return Fetched(url, 200, body, NOW, None)

    settings = ObjstoreSettings(
        _env_file=None,  # type: ignore[call-arg]
        region="us-east-1",
        access_key_id="test",
        secret_access_key="test",  # noqa: S106 - moto dummy
    )
    with mock_aws():
        make_client(settings).create_bucket(Bucket=settings.bucket_private)
        store = private_store(settings=settings)
        counts = a.ingest_recent(engine, fake, date(2026, 10, 23), archive_root=None, store=store)
        key = "raw/ieso/adequacy3/2026/10/24/PUB_Adequacy3_20261024_v68.xml.gz"
        assert counts["files"] == 1
        assert gzip.decompress(store.get_bytes(key)).decode() == xml
    with engine.connect() as conn:
        assert (
            conn.execute(
                text(
                    "SELECT archive_key FROM ieso_adequacy_files"
                    " WHERE source_file = 'PUB_Adequacy3_20261024_v68.xml'"
                )
            ).scalar_one()
            == key
        )
