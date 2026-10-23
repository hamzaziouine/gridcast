"""80% interval offsets from per-horizon residual quantiles (BUILD_PLAN §4.5, ADR-0008 ladder (i)).

Residuals are RELATIVE, r = actual / p50 - 1, so one offset fits winter and summer levels:
p10 = p50 * (1 + s * q10_h), p90 = p50 * (1 + s * q90_h), with q10_h / q90_h the empirical 10% / 90%
quantiles of r at horizon h and s the lead-time allowance (1.15 for the LightGBM bootstrap, R11;
1.0 for the baselines' trailing-60-day offsets). q10 is clamped to <= 0 and q90 to >= 0, so
p10 <= p50 <= p90 always holds.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field

import numpy as np
import pandas as pd

LEAD_TIME_ALLOWANCE = 1.15


@dataclass(frozen=True)
class IntervalOffsets:
    q10: dict[int, float]
    q90: dict[int, float]
    scale: float = 1.0
    basis: str = ""
    meta: dict[str, object] = field(default_factory=dict)

    def apply(self, p50: np.ndarray, horizon: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
        h = np.asarray(horizon, dtype=int)
        lo = np.array([self.q10.get(int(x), np.nan) for x in h])
        hi = np.array([self.q90.get(int(x), np.nan) for x in h])
        p = np.asarray(p50, dtype=np.float64)
        return p * (1 + self.scale * lo), p * (1 + self.scale * hi)

    def to_json(self) -> str:
        return json.dumps(
            {
                "q10": {str(k): v for k, v in sorted(self.q10.items())},
                "q90": {str(k): v for k, v in sorted(self.q90.items())},
                "scale": self.scale,
                "basis": self.basis,
                "meta": self.meta,
            },
            indent=1,
            sort_keys=True,
        )

    @classmethod
    def from_json(cls, raw: str | bytes) -> IntervalOffsets:
        d = json.loads(raw)
        return cls(
            q10={int(k): float(v) for k, v in d["q10"].items()},
            q90={int(k): float(v) for k, v in d["q90"].items()},
            scale=float(d["scale"]),
            basis=str(d.get("basis", "")),
            meta=dict(d.get("meta", {})),
        )


def fit_offsets(
    residuals: pd.DataFrame, scale: float = 1.0, basis: str = "", min_n: int = 30
) -> IntervalOffsets:
    """Per-horizon 10/90% quantiles of relative residuals.

    `residuals` columns: horizon, p50, actual. Horizons with fewer than `min_n` rows fall back to
    the pooled quantiles.
    """
    df = residuals.dropna(subset=["p50", "actual"])
    r = df["actual"] / df["p50"] - 1.0
    pooled_lo, pooled_hi = float(r.quantile(0.1)), float(r.quantile(0.9))
    q10: dict[int, float] = {}
    q90: dict[int, float] = {}
    for h in range(1, 25):
        rh = r[df["horizon"] == h]
        lo, hi = (
            (float(rh.quantile(0.1)), float(rh.quantile(0.9)))
            if len(rh) >= min_n
            else (pooled_lo, pooled_hi)
        )
        q10[h], q90[h] = min(lo, 0.0), max(hi, 0.0)
    return IntervalOffsets(q10, q90, scale, basis, {"n": len(df)})
