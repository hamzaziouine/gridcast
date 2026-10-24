"""Daily feature-drift rows: season-matched PSI per feature and l0_lag_parity (DATA_NOTES §19.3).

Both read `live_features` (one horizon-1 row per champion issue hour, written by the hourly job).

- `psi` (feature = name): the last 14 EST days of live rows against the champion's
  `reference_features.parquet`, season-matched (`psi.season_slice`). Needs ≥ 7 days of live rows
  and a reference file; otherwise no rows and a note. `breached` = PSI > 0.25; the context keeps
  `consecutive_days` per feature.
- `l0_lag_parity`: the mean live `mkt_l0_age_h` over the last 24 issue hours against the training
  lag REALTIME_LAG_H (plan §4.6); breached when they differ by more than 1 h (then the lag model
  needs a retrain). The context gives the share of hours that used the L0 fallback.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from typing import Any

import numpy as np
import pandas as pd
from sqlalchemy import Engine, text

from gridcast.config import REALTIME_LAG_H
from gridcast.drift import psi as psi_mod
from gridcast.storage.objstore import ObjectStore
from gridcast.timeutil import est_date

L0_PARITY_TOLERANCE_H = 1.0


def load_live(engine: Engine, lo: datetime, hi: datetime) -> pd.DataFrame:
    cols = ", ".join(psi_mod.LIVE_COLUMNS)
    with engine.connect() as conn:
        df = pd.read_sql(
            text(
                f"SELECT issue_time, {cols} FROM live_features"  # noqa: S608 - constant columns
                " WHERE issue_time >= :lo AND issue_time < :hi ORDER BY issue_time"
            ),
            conn,
            params={"lo": lo, "hi": hi},
        )
    df["issue_time"] = pd.to_datetime(df["issue_time"], utc=True)
    df["target_time"] = df["issue_time"] + pd.Timedelta(hours=1)
    return df


def _streaks(engine: Engine, run_time: datetime) -> dict[tuple[str, str], int]:
    with engine.connect() as conn:
        rows = conn.execute(
            text(
                "SELECT metric, feature, (context->>'consecutive_days')::int FROM drift_metrics"
                " WHERE metric IN ('psi', 'l0_lag_parity') AND run_time ="
                " (SELECT max(run_time) FROM drift_metrics WHERE run_time < :t)"
            ),
            {"t": run_time},
        ).all()
    return {(str(r[0]), str(r[1])): int(r[2] or 0) for r in rows}


def feature_drift(
    engine: Engine,
    store: ObjectStore,
    champion: dict[str, Any] | None,
    hi: datetime,
    run_time: datetime,
) -> tuple[list[dict[str, Any]], list[str]]:
    """drift_metrics rows (daily.write_drift shape) and notes."""
    notes: list[str] = []
    rows: list[dict[str, Any]] = []
    streak = _streaks(engine, run_time)
    live = load_live(engine, hi - timedelta(days=psi_mod.WINDOW_DAYS), hi)

    def row(
        metric: str,
        feature: str,
        value: float,
        thr: float,
        breached: bool,
        lo: datetime,
        **ctx: Any,
    ) -> dict[str, Any]:
        days = streak.get((metric, feature), 0) + 1 if breached else 0
        return {
            "rt": run_time, "ws": lo, "we": hi, "metric": metric, "feature": feature,
            "value": round(value, 6), "thr": thr, "br": breached,
            "ctx": json.dumps({"consecutive_days": days, **ctx}),
        }  # fmt: skip

    last24 = live[live["issue_time"] >= hi - timedelta(hours=24)]
    lag = last24["mkt_l0_age_h"].dropna().to_numpy(np.float64)
    if len(lag):
        mean = float(lag.mean())
        rows.append(
            row("l0_lag_parity", "", mean, float(REALTIME_LAG_H),
                abs(mean - REALTIME_LAG_H) > L0_PARITY_TOLERANCE_H, hi - timedelta(hours=24),
                training_lag_h=REALTIME_LAG_H, n=len(lag),
                share_fallback=round(float(np.mean(lag != REALTIME_LAG_H)), 4),
                max_lag_h=float(lag.max()))
        )  # fmt: skip
    else:
        notes.append("l0_lag_parity: no live_features rows in the last 24 h")

    if len(live) < psi_mod.MIN_LIVE_ROWS:
        notes.append(f"psi: {len(live)} live rows < {psi_mod.MIN_LIVE_ROWS} (needs 7 days)")
        return rows, notes
    version = (champion or {}).get("model_version")
    key = psi_mod.reference_key(str(version))
    if not version or not store.exists(key):
        notes.append(f"psi: no reference_features.parquet for {version}")
        return rows, notes
    ref = psi_mod.season_slice(
        psi_mod.from_parquet(store.get_bytes(key)),
        sorted({est_date(t) for t in live["target_time"].dt.to_pydatetime()}),
    )
    lo = hi - timedelta(days=psi_mod.WINDOW_DAYS)
    for feat in psi_mod.PSI_FEATURES:
        v = psi_mod.psi(ref[feat].to_numpy(np.float64), live[feat].to_numpy(np.float64))
        if np.isnan(v):
            continue
        rows.append(
            row("psi", feat, v, psi_mod.PSI_THRESHOLD, v > psi_mod.PSI_THRESHOLD, lo,
                n_live=len(live), n_ref=len(ref), season_days=psi_mod.SEASON_DAYS,
                reference=key)
        )  # fmt: skip
    return rows, notes
