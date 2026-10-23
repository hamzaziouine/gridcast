"""Backfill weather_hourly from the Open-Meteo Historical Forecast API, 2018 -> yesterday (C3).

Month by month, 6 cities per request, `models=gfs_seamless`, paced under half of the free limits.
Resumable: months already complete in the database are skipped, so an interrupted run is simply
started again. Writes over DATABASE_URL_DIRECT. Never prints connection strings.

    uv run python scripts/backfill_weather.py [--start 2018-01-01] [--end YYYY-MM-DD]
"""

from __future__ import annotations

import argparse
from datetime import date

import httpx
from sqlalchemy import text

from gridcast import db
from gridcast.ingest import openmeteo
from gridcast.ingest.http import TIMEOUT_S, USER_AGENT, Fetched, fetch


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--start", type=date.fromisoformat, default=openmeteo.HISTORY_START)
    parser.add_argument("--end", type=date.fromisoformat, default=None)
    args = parser.parse_args()

    engine = db.make_engine(db.migration_url())
    with httpx.Client(timeout=TIMEOUT_S, headers={"User-Agent": USER_AGENT}) as client:

        def get(url: str, params: dict[str, str]) -> Fetched:
            return fetch(url, params=params, client=client, attempts=6)

        written = openmeteo.backfill(
            engine, get, args.start, args.end, log=lambda s: print(s, flush=True)
        )

    with engine.connect() as conn:
        n, first, last = conn.execute(
            text("SELECT count(*), min(valid_time), max(valid_time) FROM weather_hourly")
        ).one()
        size = conn.execute(
            text("SELECT pg_size_pretty(pg_total_relation_size('weather_hourly'))")
        ).scalar_one()
        db_size = conn.execute(text("SELECT pg_database_size(current_database())")).scalar_one()
    print(f"written this run: {written}")
    print(f"weather_hourly rows: {n} ({first} .. {last}), size {size}")
    print(f"pg_database_size: {db_size} bytes ({db_size / 1e6:.1f} MB)")
    engine.dispose()


if __name__ == "__main__":
    main()
