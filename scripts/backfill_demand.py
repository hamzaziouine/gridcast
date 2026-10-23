"""Backfill IESO PUB_Demand 2015 -> now into raw_demand_history and actuals (C2).

Loads the newest version of each year's file with MODELLED availability (ADR-0004) over
DATABASE_URL_DIRECT (bulk load). Idempotent: a re-run inserts 0 rows and changes no stamp.

    uv run python scripts/backfill_demand.py [--from-year 2015] [--to-year 2026]

Prints per-file counts, the actuals count against 8,760 x years - 1, and the §3.1 week-ratio
diagnostic. Never prints connection strings.
"""

from __future__ import annotations

import argparse
from datetime import UTC, datetime

import httpx
import pandas as pd
from sqlalchemy import text

from gridcast import db
from gridcast.ingest import ieso_demand
from gridcast.ingest.http import TIMEOUT_S, USER_AGENT, Fetched, fetch


def main() -> None:
    now = datetime.now(UTC)
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--from-year", type=int, default=2015)
    parser.add_argument("--to-year", type=int, default=now.year)
    args = parser.parse_args()

    engine = db.make_engine(db.migration_url())
    with httpx.Client(timeout=TIMEOUT_S, headers={"User-Agent": USER_AGENT}) as client:

        def get(url: str) -> Fetched:
            return fetch(url, client=client)

        results = ieso_demand.backfill(engine, range(args.from_year, args.to_year + 1), get)

    with engine.connect() as conn:
        n_actuals, first, last = conn.execute(
            text("SELECT count(*), min(target_time), max(target_time) FROM actuals")
        ).one()
        n_raw = conn.execute(text("SELECT count(*) FROM raw_demand_history")).scalar_one()
        series = pd.read_sql(
            text("SELECT target_time, ontario_demand_mw FROM actuals ORDER BY 1"),
            conn,
            index_col="target_time",
        )["ontario_demand_mw"].astype(float)
    hours_span = int((last - first).total_seconds() // 3600) + 1
    print(f"files: {len(results)}, inserted: {sum(r.rows_inserted for r in results)}")
    print(f"raw_demand_history rows: {n_raw}")
    print(f"actuals rows: {n_actuals} (span {first} .. {last}, {hours_span} hours)")
    print(f"missing hours in span: {hours_span - n_actuals}")
    full = pd.date_range(first, last, freq="h")
    for ts in full.difference(pd.DatetimeIndex(series.index)):
        print(f"  missing {ts}")
    flagged = ieso_demand.week_ratio_outliers(series)
    print(f"week-ratio (<50%/>150%) flagged hours on PUB_Demand: {len(flagged)}")
    for ts, v in flagged.items():
        print(f"  {ts} {v}")
    engine.dispose()


if __name__ == "__main__":
    main()
