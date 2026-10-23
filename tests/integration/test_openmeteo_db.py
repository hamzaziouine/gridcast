"""C3 against real Postgres: wide weather store, idempotent upsert, resumable backfill."""

from __future__ import annotations

import json
from datetime import UTC, date, datetime
from pathlib import Path

import pytest
from sqlalchemy import Engine, text

from gridcast import db
from gridcast.ingest import openmeteo as om
from gridcast.ingest.http import Fetched

pytestmark = pytest.mark.integration

SAMPLE = (Path(__file__).parents[1] / "fixtures" / "openmeteo_historical_sample.json").read_text()


@pytest.fixture(scope="module")
def engine(fresh_db_url: str) -> Engine:
    db.upgrade(fresh_db_url)
    return db.make_engine(fresh_db_url)


def test_locations_seed_matches_code(engine: Engine) -> None:
    with engine.connect() as conn:
        rows = conn.execute(
            text("SELECT location_id, name, lat, lon, weight FROM weather_locations ORDER BY 1")
        ).all()
        cols = {
            r[0]
            for r in conn.execute(
                text(
                    "SELECT column_name FROM information_schema.columns"
                    " WHERE table_name = 'weather_hourly'"
                )
            )
        }
    assert [(r[0], r[1], float(r[2]), float(r[3]), float(r[4])) for r in rows] == [
        (c.location_id, c.name, c.lat, c.lon, c.weight) for c in om.CITIES
    ]
    assert cols == set(om.ROW_COLUMNS)


def test_backfill_is_resumable_and_idempotent(engine: Engine) -> None:
    calls: list[dict[str, str]] = []

    def fake_fetch(url: str, params: dict[str, str]) -> Fetched:
        calls.append(params)
        assert url == om.HISTORICAL_URL
        assert params["models"] == "gfs_seamless"
        return Fetched(url, 200, SAMPLE, datetime(2026, 10, 23, 21, tzinfo=UTC), None)

    limiter = om.RateLimiter(sleep=lambda s: None)
    start, end = date(2018, 1, 1), date(2018, 1, 2)
    assert om.backfill(engine, fake_fetch, start, end, limiter, log=lambda s: None) == 48
    assert om.backfill(engine, fake_fetch, start, end, limiter, log=lambda s: None) == 0
    assert len(calls) == 1  # the complete month is skipped, not re-fetched
    with engine.connect() as conn:
        n, t, avail = conn.execute(
            text(
                "SELECT count(*), max(temperature_2m_toronto) FILTER"
                " (WHERE valid_time = '2018-01-01 00:00Z'), min(available_at) FROM weather_hourly"
            )
        ).one()
    assert n == 48
    assert t == pytest.approx(-16.4)
    assert avail == datetime(2017, 12, 31, 0, tzinfo=UTC)


def test_changed_value_is_rewritten(engine: Engine) -> None:
    frame = om.parse_response(json.loads(SAMPLE))
    frame.loc[0, "temperature_2m_ottawa"] = 1.5
    with engine.begin() as conn:
        assert om.upsert_weather(conn, om.to_rows(frame, datetime.now(UTC))) == 1
    with engine.begin() as conn:
        assert om.upsert_weather(conn, om.to_rows(frame, datetime.now(UTC))) == 0
