"""IESO RealtimeTotals -> hourly Market Demand in raw_demand_history (C4).

Source facts (docs/DATA_NOTES.md §1, plan §3.0 decisions 1-2):
- One file per EST hour-ending, `PUB_RealtimeTotals_YYYYMMDDHH.csv` (HH = HE 01..24), re-posted
  every 5 minutes; the unsuffixed name is the latest version. About one month stays online.
- 4 preamble lines, then `HOUR,INTERVAL,TOTAL ENERGY,...`. No date column (it is in the file name
  and in `\\CREATED AT ... FOR yyyy/mm/dd`). No Ontario Demand.
- mean(TOTAL ENERGY) over the 12 five-minute intervals = PUB_Demand Market Demand. Only complete
  hours (12 intervals) are stored; `ontario_demand_mw` stays NULL (schema check).
- A complete hour is posted ~53-57 min after its start, so an hour is requested only once
  `target_time + REALTIME_LAG_H` has passed. Live rows are stamped `observed` (fetch time);
  `modelled` rows (re-loading the ~1 month still online) use `target_time + REALTIME_LAG_H`.
"""

from __future__ import annotations

import csv
import io
import re
from collections.abc import Callable
from datetime import date, datetime

import pandas as pd
from sqlalchemy import Engine, text

from gridcast import contracts
from gridcast.config import REALTIME_LAG_H
from gridcast.ingest.availability import realtime_totals_available_at
from gridcast.ingest.http import EmptyFeedError, Fetched
from gridcast.ingest.ieso_demand import RAW_COLUMNS, Mode, append_raw_demand
from gridcast.timeutil import HOUR, floor_hour, he_to_utc, utc_to_he

BASE_URL = "https://reports-public.ieso.ca/public/RealtimeTotals/"
SOURCE = "realtime_totals"
INTERVALS_PER_HOUR = 12

_NAME_RE = re.compile(r"^PUB_RealtimeTotals_(\d{8})(\d{2})(?:_v(\d+))?\.csv$")
_FOR_RE = re.compile(r"FOR (\d{4})/(\d{2})/(\d{2})")


def file_name_for(target_time: datetime) -> str:
    d, he = utc_to_he(target_time)
    return f"PUB_RealtimeTotals_{d:%Y%m%d}{he:02d}.csv"


def parse_file_name(name: str) -> tuple[date, int, str | None]:
    m = _NAME_RE.match(name)
    if not m:
        raise ValueError(f"not a RealtimeTotals hour file: {name!r}")
    d = datetime.strptime(m.group(1), "%Y%m%d").date()
    version = f"v{int(m.group(3)):03d}" if m.group(3) else None
    return d, int(m.group(2)), version


def parse_realtime_totals(body: str, file_name: str) -> pd.DataFrame:
    """Hourly Market Demand from one RealtimeTotals file.

    Returns one row per HOUR present: local_date, hour_ending, n_intervals, market_demand_mw
    (mean TOTAL ENERGY, 2 dp), target_time. Partial hours are returned with n_intervals < 12;
    callers keep complete hours only.
    """
    d, he, _ = parse_file_name(file_name)
    m = _FOR_RE.search(body[:400])
    if m and date(int(m.group(1)), int(m.group(2)), int(m.group(3))) != d:
        raise ValueError(f"{file_name}: FOR date {m.group(0)} does not match the file name")
    lines = body.splitlines()
    try:
        start = next(i for i, ln in enumerate(lines) if ln.startswith("HOUR,INTERVAL"))
    except StopIteration as exc:
        raise EmptyFeedError(f"{file_name}: RealtimeTotals header not found") from exc
    reader = csv.DictReader(io.StringIO("\n".join(lines[start:])))
    rows = [
        (int(r["HOUR"]), int(r["INTERVAL"]), float(r["TOTAL ENERGY"]))
        for r in reader
        if r["HOUR"] and r["HOUR"].strip().isdigit()
    ]
    if not rows:
        raise EmptyFeedError(f"{file_name}: 0 data rows")
    iv = pd.DataFrame(rows, columns=["hour_ending", "interval", "total_energy"])
    if (iv["hour_ending"] != he).any():
        raise ValueError(f"{file_name}: HOUR column does not match the file name's HE {he}")
    iv = iv.drop_duplicates(["hour_ending", "interval"], keep="last")
    out = (
        iv.groupby("hour_ending")
        .agg(n_intervals=("interval", "nunique"), market_demand_mw=("total_energy", "mean"))
        .reset_index()
    )
    out["market_demand_mw"] = out["market_demand_mw"].round(2)
    out.insert(0, "local_date", d)
    out["target_time"] = pd.to_datetime(
        [he_to_utc(d, int(h)) for h in out["hour_ending"]], utc=True
    )
    return out


def to_raw_rows(
    hourly: pd.DataFrame,
    *,
    file_name: str,
    fetched_at: datetime,
    mode: Mode,
    http_last_modified: datetime | None = None,
    lag_h: int = REALTIME_LAG_H,
) -> pd.DataFrame:
    """Complete hours only, with raw_demand_history columns and availability stamps."""
    _, _, version = parse_file_name(file_name)
    df = hourly[hourly["n_intervals"] == INTERVALS_PER_HOUR].copy()
    if mode == "modelled":
        df["available_at"] = pd.to_datetime(
            [realtime_totals_available_at(t.to_pydatetime(), lag_h) for t in df["target_time"]],
            utc=True,
        )
        df = df[df["available_at"] <= pd.Timestamp(fetched_at)].copy()
    else:
        df["available_at"] = pd.Timestamp(fetched_at)
    df["available_at_kind"] = mode
    df["source"] = SOURCE
    df["file_name"] = file_name
    df["file_version"] = version
    df["ontario_demand_mw"] = None
    df["fetched_at"] = pd.Timestamp(fetched_at)
    df["http_last_modified"] = (
        pd.Timestamp(http_last_modified) if http_last_modified is not None else pd.NaT
    )
    return df[list(RAW_COLUMNS)].reset_index(drop=True)


def ingest_recent(
    engine: Engine,
    fetch: Callable[[str], Fetched],
    now: datetime,
    lookback_h: int = 6,
    mode: Mode = "observed",
    lag_h: int = REALTIME_LAG_H,
) -> dict[str, int]:
    """Fetch the last `lookback_h` hours that should be complete and are not stored yet.

    An hour is due when `target_time + lag_h <= now`. Missing (404) and partial hours are counted
    and skipped, never fatal (plan §3.0 decision 6: the forecast falls back to the last complete
    hour). Returns counts: due, stored_before, fetched, inserted, missing, partial.
    """
    newest = floor_hour(now) - lag_h * HOUR
    due = [newest - k * HOUR for k in range(lookback_h)]
    with engine.connect() as conn:
        have = {
            r[0]
            for r in conn.execute(
                text(
                    "SELECT DISTINCT target_time FROM raw_demand_history"
                    " WHERE source = 'realtime_totals' AND target_time >= :lo"
                ),
                {"lo": min(due)},
            )
        }
    counts = dict.fromkeys(("due", "stored_before", "fetched", "inserted", "missing", "partial"), 0)
    counts["due"] = len(due)
    for t in sorted(due):
        if t in have:
            counts["stored_before"] += 1
            continue
        fetched = fetch(BASE_URL + file_name_for(t))
        if fetched.status == 404:
            counts["missing"] += 1
            continue
        counts["fetched"] += 1
        hourly = parse_realtime_totals(fetched.text, fetched.name)
        rows = to_raw_rows(
            hourly,
            file_name=fetched.name,
            fetched_at=fetched.fetched_at,
            mode=mode,
            http_last_modified=fetched.last_modified,
            lag_h=lag_h,
        )
        if rows.empty:
            counts["partial"] += 1
            continue
        contracts.RawDemand.validate(rows)
        with engine.begin() as conn:
            counts["inserted"] += len(append_raw_demand(conn, rows))
    return counts
