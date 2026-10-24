"""Live Open-Meteo snapshots: archive to the private bucket + `weather_snapshot_index` (C10).

BUILD_PLAN §6.1 step 5, §13 #5, ADR-0004 catch-up policy:
- One snapshot per run slot (the UTC hour it was fetched in): Parquet at
  `weather/snapshots/YYYY/MM/DD/HH.parquet` (valid_time + the 54 weather columns, 72 rows:
  slot - 24 h .. slot + 47 h) and an index row with `available_at = fetched_at` (observed).
- The first snapshot of a slot is kept; a later run in the same slot does not fetch again.
- An issue hour H may use a snapshot only if `available_at <= data_cutoff(H)` and it covers
  H - 23 h (the 24 h rolling weather features) .. H + 24 h. The newest such snapshot wins. The
  same rule serves the current hour and catch-up hours, so a missed hour never sees a forecast
  fetched after its cutoff; with no qualifying snapshot the champion is not run for H.
"""

from __future__ import annotations

import io
from collections.abc import Callable
from datetime import datetime, timedelta

import pandas as pd
from sqlalchemy import Engine, text

from gridcast.ingest.availability import data_cutoff, live_snapshot_available_at
from gridcast.storage.objstore import ObjectStore
from gridcast.timeutil import floor_hour
from gridcast.weather import WEATHER_COLUMNS

PREFIX = "weather/snapshots/"
NEEDS_BEFORE = timedelta(hours=23)
NEEDS_AFTER = timedelta(hours=24)

FetchSnapshot = Callable[[], tuple[pd.DataFrame, datetime]]


def snapshot_key(slot: datetime) -> str:
    return f"{PREFIX}{slot:%Y/%m/%d/%H}.parquet"


def has_slot(engine: Engine, slot: datetime) -> bool:
    with engine.connect() as conn:
        return (
            conn.execute(
                text("SELECT 1 FROM weather_snapshot_index WHERE issue_time = :s"), {"s": slot}
            ).first()
            is not None
        )


def store_snapshot(
    engine: Engine, store: ObjectStore, frame: pd.DataFrame, fetched_at: datetime
) -> str:
    """Archive one validated live snapshot; returns its key. Idempotent per slot."""
    slot = floor_hour(fetched_at)
    key = snapshot_key(slot)
    cols = ["valid_time", *WEATHER_COLUMNS]
    buf = io.BytesIO()
    frame[cols].to_parquet(buf, index=False)
    with engine.begin() as conn:
        inserted = conn.execute(
            text(
                "INSERT INTO weather_snapshot_index (issue_time, key, n_rows, span_start,"
                " span_end, fetched_at, available_at) VALUES (:s, :k, :n, :lo, :hi, :f, :a)"
                " ON CONFLICT (issue_time) DO NOTHING RETURNING issue_time"
            ),
            {
                "s": slot,
                "k": key,
                "n": len(frame),
                "lo": frame["valid_time"].min().to_pydatetime(),
                "hi": frame["valid_time"].max().to_pydatetime(),
                "f": fetched_at,
                "a": live_snapshot_available_at(fetched_at),
            },
        ).first()
        if inserted is not None:  # upload inside the transaction: no index row without object
            store.put_bytes(key, buf.getvalue(), "application/vnd.apache.parquet")
    return key


def fetch_and_store(
    engine: Engine, store: ObjectStore, fetch_snapshot: FetchSnapshot, now: datetime
) -> str:
    """Fetch the live snapshot unless this slot already has one. Returns 'stored' or 'exists'."""
    if has_slot(engine, floor_hour(now)):
        return "exists"
    frame, fetched_at = fetch_snapshot()
    store_snapshot(engine, store, frame, fetched_at)
    return "stored"


def qualifying(engine: Engine, store: ObjectStore, issue_time: datetime) -> pd.DataFrame | None:
    """The weather frame issue_time may use (with `available_at`), or None (module docstring)."""
    with engine.connect() as conn:
        row = conn.execute(
            text(
                "SELECT key, available_at FROM weather_snapshot_index WHERE available_at <= :c"
                " AND span_start <= :lo AND span_end >= :hi ORDER BY available_at DESC LIMIT 1"
            ),
            {
                "c": data_cutoff(issue_time),
                "lo": issue_time - NEEDS_BEFORE,
                "hi": issue_time + NEEDS_AFTER,
            },
        ).first()
    if row is None:
        return None
    frame = pd.read_parquet(io.BytesIO(store.get_bytes(str(row[0]))))
    frame["valid_time"] = pd.to_datetime(frame["valid_time"], utc=True)
    frame["available_at"] = pd.Timestamp(row[1]).tz_convert("UTC")
    return frame
