"""`gridcast run prune` (C16, BUILD_PLAN §4.4 levers 1 and 3, §6.8; DATA_NOTES §14.4, §20.3).

Retention rules, and nothing else:

1. **Weak names, 90 days.** `seasonal_naive_24`, `persistence` and `prev_champion` rows with
   `issue_time` older than 90 days: their `scores` rows (same model_id, issue_time = target_time -
   horizon), their `predictions` rows and their `prediction_runs` rows go in **one transaction**,
   scores first, so `v_scores` never holds a score whose prediction is gone and a prediction
   never outlives its run row (FK). `score_daily` keeps their daily aggregates: it is rebuilt by
   the daily job for the trailing 45 days only, so a 90-day prune never changes it
   (`PRUNE_DAYS > daily.SCORE_DAYS`, unit-tested). champion, shadow, seasonal_naive_168 and
   ieso_adequacy are never pruned.
2. **RealtimeTotals history, 60 days.** `raw_demand_history` rows with source `realtime_totals`
   and `target_time` older than 60 days, except the newest version (latest `available_at`) of each
   hour. PUB_Demand rows are never touched.
3. `VACUUM (ANALYZE)` of the touched tables over DATABASE_URL_DIRECT, only when rows were deleted.

**Dry run by default**: counts what each rule would delete, deletes nothing (`pipeline_runs`
status `skipped`, note `dry-run`). `--apply` deletes (status `ok`, counts in `notes`, Healthchecks
ping). Both report `pg_database_size` and the size of the main tables.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import Connection, Engine, text

from gridcast.config import get_settings
from gridcast.jobs import runs
from gridcast.timeutil import floor_hour

JOB = "prune"
PRUNE_DAYS = 90
WEAK_MODELS = ("seasonal_naive_24", "persistence", "prev_champion")
REALTIME_DAYS = 60
TABLES = (
    "predictions",
    "prediction_runs",
    "scores",
    "raw_demand_history",
    "ieso_forecast_snapshots",
    "pipeline_runs",
    "weather_hourly",
    "actuals",
)

# Constant SQL only (no user input): count for the dry run, delete for --apply.
_WEAK = "model_id IN (SELECT model_id FROM prediction_models WHERE model_name = ANY(:m))"
_WHERE = {
    "scores": f"FROM scores WHERE {_WEAK} AND target_time - horizon * interval '1 hour' < :cut",
    "predictions": f"FROM predictions WHERE {_WEAK} AND issue_time < :cut",
    "prediction_runs": f"FROM prediction_runs WHERE {_WEAK} AND issue_time < :cut",
}
_COUNT = {k: "SELECT count(*) " + v for k, v in _WHERE.items()}
_DELETE = {k: "DELETE " + v for k, v in _WHERE.items()}
_REALTIME_OLD = """
SELECT r.target_time, r.file_name FROM (
  SELECT target_time, file_name,
         row_number() OVER (PARTITION BY target_time
                            ORDER BY available_at DESC, file_name DESC) AS rn
  FROM raw_demand_history WHERE source = 'realtime_totals' AND target_time < :rcut
) r WHERE r.rn > 1
"""
_REALTIME_COUNT = f"SELECT count(*) FROM ({_REALTIME_OLD}) x"  # noqa: S608 - constant SQL
_REALTIME_DELETE = f"""
DELETE FROM raw_demand_history h USING ({_REALTIME_OLD}) old
WHERE h.source = 'realtime_totals' AND h.target_time = old.target_time
  AND h.file_name = old.file_name
"""  # noqa: S608 - constant SQL


def sizes(conn: Connection) -> dict[str, Any]:
    out: dict[str, Any] = {
        "database_bytes": int(
            conn.execute(text("SELECT pg_database_size(current_database())")).scalar_one()
        )
    }
    for t in TABLES:
        out[t] = int(conn.execute(text("SELECT pg_total_relation_size(:t)"), {"t": t}).scalar_one())
    return out


def plan(conn: Connection, now: datetime) -> dict[str, Any]:
    cut = floor_hour(now) - timedelta(days=PRUNE_DAYS)
    rcut = floor_hour(now) - timedelta(days=REALTIME_DAYS)
    p = {"m": list(WEAK_MODELS), "cut": cut}
    counts = {k: int(conn.execute(text(q), p).scalar_one()) for k, q in _COUNT.items()}
    counts["raw_demand_history_realtime"] = int(
        conn.execute(text(_REALTIME_COUNT), {"rcut": rcut}).scalar_one()
    )
    return {"weak_cutoff": cut.isoformat(), "realtime_cutoff": rcut.isoformat(), "rows": counts}


def apply(conn: Connection, now: datetime) -> dict[str, int]:
    cut = floor_hour(now) - timedelta(days=PRUNE_DAYS)
    rcut = floor_hour(now) - timedelta(days=REALTIME_DAYS)
    p = {"m": list(WEAK_MODELS), "cut": cut}
    done = {}
    for k in ("scores", "predictions", "prediction_runs"):  # FK and v_scores order
        done[k] = int(conn.execute(text(_DELETE[k]), p).rowcount or 0)
    done["raw_demand_history_realtime"] = int(
        conn.execute(text(_REALTIME_DELETE), {"rcut": rcut}).rowcount or 0
    )
    return done


def vacuum(direct: Engine, tables: list[str]) -> None:
    with direct.connect() as conn:
        for t in tables:
            conn.execute(text(f"VACUUM (ANALYZE) {t}"))  # constant table names


def run(
    engine: Engine,
    direct: Callable[[], Engine] | None,
    apply_changes: bool = False,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
    ping: runs.Pinger = runs.http_get,
    run_kind: str = "manual",
) -> dict[str, Any]:
    t = now()
    run_id = runs.start(engine, JOB, floor_hour(t), run_kind)
    notes = runs.Notes()
    out: dict[str, Any] = {"mode": "apply" if apply_changes else "dry-run", "run_kind": run_kind}
    status = "ok" if apply_changes else "skipped"
    try:
        with engine.connect() as conn:
            out["before"] = sizes(conn)
            out.update(plan(conn, t))
        if apply_changes:
            with engine.begin() as conn:
                out["deleted"] = apply(conn, t)
            touched = [k for k, v in out["deleted"].items() if v]
            if touched and direct is not None:
                tables = sorted(
                    {"raw_demand_history" if k.startswith("raw") else k for k in touched}
                )
                eng = direct()
                try:
                    vacuum(eng, tables)
                finally:
                    eng.dispose()
                out["vacuumed"] = tables
            with engine.connect() as conn:
                out["after"] = sizes(conn)
        else:
            notes.add("dry-run")
        notes.add(json.dumps(out.get("deleted", out["rows"]), separators=(",", ":")))
    except Exception as exc:
        notes.error("prune", exc)
        status = "failed"
    out["status"] = runs.finish(engine, run_id, status, sum(out.get("deleted", {}).values()),
                                notes.private)  # fmt: skip
    out["notes"] = notes.public
    hc = get_settings().healthchecks_prune_url
    out["heartbeat"] = (
        runs.heartbeat(hc, out["status"] != "failed", ping)
        if apply_changes or out["status"] == "failed"
        else "dry-run"
    )
    return out
