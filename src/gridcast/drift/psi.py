"""Season-matched PSI (BUILD_PLAN §6.2 step 4; DATA_NOTES §19.3).

- Features: the plan's ~8 weather and level features (calendar features are excluded: the
  calendar fixes their distribution). Values are the horizon-1 rows: one per issue hour.
- Reference: the champion's training rows (`reference_features.parquet` next to the model, built
  by retrain or `gridcast drift reference`), restricted to target days whose day of year is within
  ±15 days of a day in the live window. Ten quantile bins from that slice, epsilon 1e-4.
- Live: the last 14 days of `live_features` (what the hourly job actually served).
- PSI is a diagnostic. On its own it never triggers a retrain; only PSI > 0.25 on ≥ 2 features
  for 3 days together with a 7-day MAPE above 1.2 x the reference does (`daily.retrain_trigger`).

Implemented with numpy rather than Evidently (the plan's library): the statistic is ten lines and
Evidently would add a large dependency to every job's install.
"""

from __future__ import annotations

import io
from datetime import date

import numpy as np
import pandas as pd

from gridcast.features.build import FeatureBuilder

PSI_FEATURES: tuple[str, ...] = (
    "temperature_2m_w",
    "apparent_temperature_w",
    "dew_point_2m_w",
    "hdd_w",
    "cdd_w",
    "shortwave_radiation_w",
    "wind_speed_10m_w",
    "ont_lag168",
)
LIVE_COLUMNS: tuple[str, ...] = ("mkt_l0_age_h", *PSI_FEATURES)
PSI_THRESHOLD = 0.25
BINS = 10
EPS = 1e-4
SEASON_DAYS = 15
WINDOW_DAYS = 14
MIN_LIVE_ROWS = 7 * 24


def psi(reference: np.ndarray, current: np.ndarray, bins: int = BINS, eps: float = EPS) -> float:
    """Population stability index of `current` against `reference`, quantile bins of the
    reference (edges deduplicated, open-ended; a value equal to an edge goes to the lower bin, so a
    point mass such as cdd_w = 0 all winter is one bin)."""
    ref = reference[~np.isnan(reference)]
    cur = current[~np.isnan(current)]
    if len(ref) == 0 or len(cur) == 0:
        return float("nan")
    edges = np.unique(np.quantile(ref, np.linspace(0, 1, bins + 1)[1:-1]))
    e = np.bincount(np.searchsorted(edges, ref, side="left"), minlength=len(edges) + 1)
    a = np.bincount(np.searchsorted(edges, cur, side="left"), minlength=len(edges) + 1)
    e_share = np.maximum(e / e.sum(), eps)
    a_share = np.maximum(a / a.sum(), eps)
    return float(np.sum((a_share - e_share) * np.log(a_share / e_share)))


def season_slice(
    reference: pd.DataFrame, days: list[date], window: int = SEASON_DAYS
) -> pd.DataFrame:
    """Reference rows whose target day of year is within ±window of any of `days` (wraps at the
    year end)."""
    ref_doy = pd.DatetimeIndex(reference["target_time"]).day_of_year.to_numpy()
    keep = np.zeros(len(reference), dtype=bool)
    for doy in sorted({d.timetuple().tm_yday for d in days}):
        dist = np.abs(ref_doy - doy)
        keep |= np.minimum(dist, 365 - dist) <= window
    return reference[keep]


def reference_frame(fb: FeatureBuilder, issues: pd.DatetimeIndex) -> pd.DataFrame:
    """Horizon-1 rows (target_time + the PSI features) for the training issue hours."""
    fr = fb.frame(issues, [1])
    return fr[["target_time", *PSI_FEATURES]].reset_index(drop=True)


def to_parquet(frame: pd.DataFrame) -> bytes:
    out = frame.copy()
    for c in PSI_FEATURES:
        out[c] = out[c].astype(np.float32)
    buf = io.BytesIO()
    out.to_parquet(buf, index=False)
    return buf.getvalue()


def from_parquet(raw: bytes) -> pd.DataFrame:
    df = pd.read_parquet(io.BytesIO(raw))
    df["target_time"] = pd.to_datetime(df["target_time"], utc=True)
    return df


def reference_key(model_version: str) -> str:
    return f"models/{model_version}/reference_features.parquet"


def build_reference(raw: pd.DataFrame, weather: pd.DataFrame, actuals: pd.Series) -> pd.DataFrame:
    """The training distribution of a model fitted on this data: horizon-1 rows for every hourly
    issue time from TRAIN_START to the last one whose targets have actuals and weather."""
    from gridcast.backtest.harness import TRAIN_START
    from gridcast.features.build import DemandHistory

    end = min(
        actuals.index.max() - pd.Timedelta(hours=1),
        pd.Timestamp(weather["valid_time"].max()) - pd.Timedelta(hours=24),
    )
    issues = pd.date_range(TRAIN_START, end, freq="h", tz="UTC")
    return reference_frame(FeatureBuilder(DemandHistory.from_raw(raw), weather), issues)
