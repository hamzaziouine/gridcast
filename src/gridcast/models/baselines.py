"""Naive baselines on the same as-of demand history as the model (C7, BUILD_PLAN §4.5).

All three forecast Ontario Demand and read it under the PUB_Demand availability rule:
- seasonal_naive_168: demand at target - 168 h (always available: 168 h > 24 h + the 32 h PUB lag).
- seasonal_naive_24: demand at target - 24 h if available at the cutoff, else - 48 h, else - 72 h.
  Under the PUB rule (8-32 h old) the 24 h lag is rarely available, and at early-morning issue
  times even - 48 h is not, hence the third step (the plan lists 24 -> 48 only).
- persistence: the newest Ontario Demand hour available at the cutoff, for every horizon.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from gridcast.features.build import CUTOFF_NS, H_NS, NEG, DemandHistory, from_ns

BASELINES: tuple[str, ...] = ("seasonal_naive_168", "seasonal_naive_24", "persistence")
PERSISTENCE_SEARCH_H = 72


def baseline_p50(
    name: str, history: DemandHistory, issue_ns: np.ndarray, horizon: int
) -> tuple[np.ndarray, np.ndarray]:
    """(p50, input available_at in ns, NEG when no input) for each issue time at one horizon."""
    cut = issue_ns + CUTOFF_NS
    tgt = issue_ns + horizon * H_NS
    on = history.ontario
    if name == "seasonal_naive_168":
        return on.lookup(tgt - 168 * H_NS, cut)
    if name == "seasonal_naive_24":
        v = np.full(len(issue_ns), np.nan)
        a = np.full(len(issue_ns), NEG, dtype=np.int64)
        for lag in (24, 48, 72):
            lv, la = on.lookup(tgt - lag * H_NS, cut)
            take = np.isnan(v) & ~np.isnan(lv)
            v[take], a[take] = lv[take], la[take]
        return v, a
    if name == "persistence":
        offs = np.arange(1, PERSISTENCE_SEARCH_H + 1)
        mv, ma = on.lookup(issue_ns[:, None] - offs[None, :] * H_NS, cut[:, None])
        vis = ma != NEG
        j = np.argmax(vis, axis=1)
        has = vis.any(axis=1)
        rows = np.arange(len(issue_ns))
        return np.where(has, mv[rows, j], np.nan), np.where(has, ma[rows, j], NEG)
    raise ValueError(f"unknown baseline {name!r}")


def baseline_frame(
    history: DemandHistory, issue_ns: np.ndarray, horizons: range | list[int] = range(1, 25)
) -> pd.DataFrame:
    """Long frame issue_time, target_time, horizon, model_name, p50, input_available_at."""
    parts = []
    for name in BASELINES:
        for h in horizons:
            p50, avail = baseline_p50(name, history, issue_ns, h)
            parts.append(
                pd.DataFrame(
                    {
                        "issue_time": from_ns(issue_ns),
                        "target_time": from_ns(issue_ns + h * H_NS),
                        "horizon": h,
                        "model_name": name,
                        "p50": p50,
                        "input_available_at": from_ns(np.where(avail == NEG, 0, avail)),
                    }
                )
            )
    return pd.concat(parts, ignore_index=True)
