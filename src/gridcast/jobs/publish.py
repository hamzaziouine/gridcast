"""Public JSON snapshots in the public bucket (`gridcast-public`), read by the Worker (§3.0 item 8).

Keys and schema (schema_version 1; every timestamp ISO-8601 UTC with `Z`; MW rounded to 0.1):

- `latest/forecast.json` (hourly): the newest issue hour with champion rows.
  {schema_version, issue_time, data_cutoff, model_version, run_kind, units, target,
   rows: [{horizon, target_time, p10, p50, p90,
           ieso_adequacy, ieso_staleness_min, seasonal_naive_168, seasonal_naive_24, persistence}
          x 24], attribution}
  The benchmark columns are the p50 of the same issue time (null when not available).
- `latest/meta.json` (hourly): {schema_version, generated_at, last_issue_time, stale_after_minutes,
  champion {model_name, model_version, champion_status, bootstrap_reason, promoted_at,
  feature_schema_version}, last_run {job, logical_time, status, run_kind, notes[]},
  inputs {newest_realtime_hour, newest_adequacy_created_at, newest_actual,
  newest_weather_snapshot}, keys {forecast, scoreboard}, attribution}
- `latest/scoreboard.json` (daily): built by `jobs/daily.py` (schema in its docstring).

The Worker serves these at /v1/forecast/latest, /v1/scoreboard and /health; it holds no database
credential. Nothing here is secret: values come from the public tables only.
"""

from __future__ import annotations

import json
import math
from datetime import datetime
from typing import Any

import pandas as pd
from sqlalchemy import Engine, text

from gridcast.config import DATA_CUTOFF_OFFSET
from gridcast.storage.objstore import ObjectStore

SCHEMA_VERSION = 1
FORECAST_KEY = "latest/forecast.json"
META_KEY = "latest/meta.json"
SCOREBOARD_KEY = "latest/scoreboard.json"
STALE_AFTER_MIN = 90
BENCHMARKS = ("ieso_adequacy", "seasonal_naive_168", "seasonal_naive_24", "persistence")
TARGET = "Ontario Demand (IESO PUB_Demand), MW; target_time = start of the hour, UTC"
ATTRIBUTION = (
    "Demand data and the IESO forecast: Independent Electricity System Operator (IESO), "
    "used under the IESO Terms of Use",
    "Weather: Open-Meteo.com, CC BY 4.0",
)
CHAMPION_FIELDS = (
    "model_name",
    "model_version",
    "champion_status",
    "bootstrap_reason",
    "promoted_at",
    "feature_schema_version",
)


def iso(ts: Any) -> str | None:
    if ts is None or (isinstance(ts, float) and math.isnan(ts)) or ts is pd.NaT:
        return None
    return pd.Timestamp(ts).tz_convert("UTC").strftime("%Y-%m-%dT%H:%M:%SZ")


def num(x: Any, digits: int = 1) -> float | None:
    if x is None:
        return None
    f = float(x)
    return None if math.isnan(f) else round(f, digits)


def put(store: ObjectStore, key: str, doc: dict[str, Any]) -> int:
    """Write compact JSON; returns its size in bytes."""
    body = json.dumps(doc, separators=(",", ":"), allow_nan=False).encode("utf-8")
    store.put_bytes(key, body, "application/json")
    return len(body)


def forecast_doc(engine: Engine) -> dict[str, Any] | None:
    """The newest champion issue, with its benchmarks; None before the first champion row."""
    with engine.connect() as conn:
        issue = conn.execute(
            text("SELECT max(issue_time) FROM prediction_runs WHERE model_id = 1")
        ).scalar()
        if issue is None:
            return None
        df = pd.read_sql(
            text(
                "SELECT horizon, target_time, model_name, model_version, p10, p50, p90,"
                " ieso_staleness_min, run_kind FROM v_predictions WHERE issue_time = :i"
            ),
            conn,
            params={"i": issue},
        )
    champ = df[df["model_name"] == "champion"].sort_values("horizon")
    bench = {str(n): g.set_index("horizon") for n, g in df.groupby("model_name")}
    rows = []
    for r in champ.to_dict("records"):
        h = int(r["horizon"])
        row: dict[str, Any] = {
            "horizon": h,
            "target_time": iso(r["target_time"]),
            "p10": num(r["p10"]),
            "p50": num(r["p50"]),
            "p90": num(r["p90"]),
        }
        for name in BENCHMARKS:
            g = bench.get(name)
            row[name] = num(g["p50"].get(h)) if g is not None else None
        ieso = bench.get("ieso_adequacy")
        stale = ieso["ieso_staleness_min"].get(h) if ieso is not None else None
        minutes = num(stale, 0)
        row["ieso_staleness_min"] = None if minutes is None else int(minutes)
        rows.append(row)
    return {
        "schema_version": SCHEMA_VERSION,
        "issue_time": iso(issue),
        "data_cutoff": iso(pd.Timestamp(issue) + DATA_CUTOFF_OFFSET),
        "model_version": str(champ["model_version"].iloc[0]) if len(champ) else None,
        "run_kind": str(champ["run_kind"].iloc[0]) if len(champ) else None,
        "units": "MW",
        "target": TARGET,
        "rows": rows,
        "attribution": list(ATTRIBUTION),
    }


def _inputs(engine: Engine) -> dict[str, str | None]:
    sql = {
        "newest_realtime_hour": "SELECT max(target_time) FROM raw_demand_history"
        " WHERE source = 'realtime_totals'",
        "newest_adequacy_created_at": "SELECT max(created_at) FROM ieso_forecast_snapshots",
        "newest_actual": "SELECT max(target_time) FROM actuals",
        "newest_weather_snapshot": "SELECT max(fetched_at) FROM weather_snapshot_index",
    }
    with engine.connect() as conn:
        return {k: iso(conn.execute(text(q)).scalar()) for k, q in sql.items()}


def meta_doc(
    engine: Engine,
    champion: dict[str, Any] | None,
    last_issue: str | None,
    run: dict[str, Any],
    now: datetime,
) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "generated_at": iso(now),
        "last_issue_time": last_issue,
        "stale_after_minutes": STALE_AFTER_MIN,
        "champion": {k: (champion or {}).get(k) for k in CHAMPION_FIELDS},
        "last_run": run,
        "inputs": _inputs(engine),
        "keys": {"forecast": FORECAST_KEY, "scoreboard": SCOREBOARD_KEY},
        "attribution": list(ATTRIBUTION),
    }


def publish_hourly(
    engine: Engine,
    public: ObjectStore,
    champion: dict[str, Any] | None,
    run: dict[str, Any],
    now: datetime,
) -> dict[str, int]:
    """forecast.json (if any champion row exists) + meta.json; returns key -> bytes."""
    sizes: dict[str, int] = {}
    fc = forecast_doc(engine)
    if fc is not None:
        sizes[FORECAST_KEY] = put(public, FORECAST_KEY, fc)
    last_issue = fc["issue_time"] if fc else None
    sizes[META_KEY] = put(public, META_KEY, meta_doc(engine, champion, last_issue, run, now))
    return sizes
