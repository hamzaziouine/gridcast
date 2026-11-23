"""`gridcast repair ...`: data repairs, run from `repair.yml` so each one is in the Actions log.

BUILD_PLAN §12: live tables are never edited by hand; a repair is a CLI command run from a
`workflow_dispatch`, and its `docs/OPS_LOG.md` entry is tagged INTERVENTION.

`ieso_rows`: rewrite the `ieso_adequacy` rows of past issue hours from the Adequacy3 snapshots now
stored, with the same as-of rule the hourly job uses (`predict.ieso_rows`: per target, the latest
snapshot created at or before the issue's data_cutoff). Needed when the ingest read versions late
(10-26 to 11-01: capped at 20 files a run, oldest first, ~5 runs a day), so the row written at
the time used an older snapshot than IESO had published (DATA_NOTES §21.2). Only issue hours that
already have an IESO run row are touched: the set of scored hours does not grow. Champion rows
are never read or written. Dry run by default.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import numpy as np
import pandas as pd
from sqlalchemy import Engine, text

from gridcast.jobs import predict, runs
from gridcast.timeutil import floor_hour

JOB_IESO = "repair_ieso_rows"
COLUMNS = [
    "issue_time", "target_time", "horizon", "model_name", "model_version", "p10", "p50", "p90"
]  # fmt: skip


def _stored(engine: Engine, lo: datetime, hi: datetime) -> pd.DataFrame:
    with engine.connect() as conn:
        df = pd.read_sql(
            text(
                "SELECT r.issue_time, p.horizon, p.p50::float8 AS p50, v.model_version"
                " FROM prediction_runs r"
                " JOIN prediction_models m ON m.model_id = r.model_id"
                " LEFT JOIN predictions p ON p.issue_time = r.issue_time"
                "  AND p.model_id = r.model_id"
                " LEFT JOIN model_versions v ON v.version_id = p.version_id"
                " WHERE m.model_name = 'ieso_adequacy' AND r.issue_time BETWEEN :lo AND :hi"
            ),
            conn,
            params={"lo": lo, "hi": hi},
        )
    df["issue_time"] = pd.to_datetime(df["issue_time"], utc=True)
    return df


def _actuals(engine: Engine, lo: datetime, hi: datetime) -> pd.Series:
    with engine.connect() as conn:
        df = pd.read_sql(
            text(
                "SELECT target_time, ontario_demand_mw::float8 AS actual FROM actuals"
                " WHERE target_time > :lo AND target_time <= :hi"
            ),
            conn,
            params={"lo": lo, "hi": hi + timedelta(hours=24)},
        )
    df["target_time"] = pd.to_datetime(df["target_time"], utc=True)
    return df.set_index("target_time")["actual"]


def _mape(p50: pd.Series, targets: pd.Series, actual: pd.Series) -> tuple[float | None, int]:
    a = actual.reindex(pd.DatetimeIndex(targets)).to_numpy(np.float64)
    p = p50.to_numpy(np.float64)
    ok = ~np.isnan(a) & ~np.isnan(p)
    if not ok.any():
        return None, 0
    return round(float(np.mean(np.abs(p[ok] - a[ok]) / a[ok]) * 100), 3), int(ok.sum())


def ieso_rows(
    engine: Engine, start: datetime, end: datetime, apply: bool = False
) -> dict[str, Any]:
    lo, hi = floor_hour(start.astimezone(UTC)), floor_hour(end.astimezone(UTC))
    stored = _stored(engine, lo, hi)
    issues = sorted(set(stored["issue_time"]))
    have = stored.dropna(subset=["horizon"])
    by_issue: dict[pd.Timestamp, dict[int, tuple[float, str]]] = {i: {} for i in issues}
    for i, h, p50, v in zip(
        have["issue_time"], have["horizon"].to_numpy(int), have["p50"].to_numpy(np.float64),
        have["model_version"].to_numpy(str), strict=True,
    ):  # fmt: skip
        by_issue[i][int(h)] = (float(p50), str(v))
    counts = {
        "issue_hours": len(issues),
        "issue_hours_changed": 0,
        "rows_unchanged": 0,
        "rows_changed": 0,
        "rows_new": 0,
        "rows_kept_without_snapshot": 0,
        "rows_written": 0,
    }
    old_parts: list[pd.DataFrame] = []
    new_parts: list[pd.DataFrame] = []
    run_id = None
    if apply:
        run_id = runs.start(engine, JOB_IESO, floor_hour(datetime.now(UTC)), "manual")
    for issue in issues:
        fresh = predict.ieso_rows(engine, issue.to_pydatetime())
        known = by_issue[issue]
        horizons = [int(h) for h in fresh["horizon"].to_numpy(int)]
        counts["rows_kept_without_snapshot"] += len(set(known) - set(horizons))
        changed = False
        for h, p50, v in zip(
            horizons, fresh["p50"].to_numpy(np.float64), fresh["model_version"].to_numpy(str),
            strict=True,
        ):  # fmt: skip
            was = known.get(h)
            if was is None:
                counts["rows_new"] += 1
                changed = True
            elif was[1] != str(v) or abs(was[0] - float(p50)) > 0.005:
                counts["rows_changed"] += 1
                changed = True
            else:
                counts["rows_unchanged"] += 1
        if fresh.empty:
            continue
        old = have[have["issue_time"] == issue]
        old_parts.append(
            pd.DataFrame(
                {
                    "target_time": old["issue_time"] + pd.to_timedelta(old["horizon"], "h"),
                    "p50": old["p50"],
                }
            )
        )
        new_parts.append(fresh[["target_time", "p50"]])
        if changed:
            counts["issue_hours_changed"] += 1
            if run_id is not None:
                written = predict.write_predictions(engine, fresh[COLUMNS], run_id, "manual")
                counts["rows_written"] += sum(written.values())
    out: dict[str, Any] = {"window": [lo.isoformat(), hi.isoformat()], "apply": apply, **counts}
    if old_parts:
        actual = _actuals(engine, lo, hi)
        old_all, new_all = pd.concat(old_parts), pd.concat(new_parts)
        mape_old, n_old = _mape(old_all["p50"], old_all["target_time"], actual)
        mape_new, n_new = _mape(new_all["p50"], new_all["target_time"], actual)
        out["ieso_mape_stored"] = {"mape": mape_old, "n": n_old}
        out["ieso_mape_recomputed"] = {"mape": mape_new, "n": n_new}
    if run_id is not None:
        out["status"] = runs.finish(
            engine, run_id, "ok", counts["rows_written"], [f"{k}={v}" for k, v in counts.items()]
        )
    return out
