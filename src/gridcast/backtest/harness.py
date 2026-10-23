"""Rolling-origin backtest (C7/C8, BUILD_PLAN §7.3).

- 12 folds, one per EST calendar month, ending with the last month whose test targets all have
  final actuals. A fold trains on every target hour before `fold start - 2 days`.
- Test issue times: every 6 h (00/06/12/18Z) inside the fold, 24 horizons each.
- Training issue times: hourly from TRAIN_START (weather starts 2018-01-01; the first issue whose
  24 h weather window is complete is 2018-01-02 00Z).
- Leakage assertion: every feature row of every fold satisfies input_available_at <= data_cutoff
  (`features.build.assert_asof`, run on the train and test rows of each fold).
- Lag parity log: per fold, the distribution of issue_time - L0 hour and of the weather margin
  (data_cutoff - newest weather input).
- Peak hours (§4.3 `peak_hours`): P90 (linear interpolation, = percentile_cont) of final actuals
  over the 30 EST days ending the day before the target; requires all 720 hours.
- Intervals: LightGBM offsets come from folds 1-10 residuals x 1.15 and coverage is reported on the
  held-out folds 11-12 only (§4.5); baselines use the trailing 60 days of their own residuals
  before each fold's training cutoff.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from typing import Any

import numpy as np
import pandas as pd
from sqlalchemy import Engine, text

from gridcast.features.build import (
    CUTOFF_NS,
    H_NS,
    DemandHistory,
    FeatureBuilder,
    from_ns,
    to_ns,
)
from gridcast.models.baselines import BASELINES, baseline_frame
from gridcast.models.intervals import IntervalOffsets, fit_offsets
from gridcast.timeutil import EST, est_date, he_to_utc

TRAIN_START = datetime(2018, 1, 2, tzinfo=UTC)
TRAIN_GAP = timedelta(days=2)
TEST_STEP_H = 6
BASELINE_INTERVAL_WINDOW = timedelta(days=60)
HELDOUT_FOLDS = (11, 12)
SEASONS = {12: "winter", 1: "winter", 2: "winter", 3: "spring", 4: "spring", 5: "spring",
           6: "summer", 7: "summer", 8: "summer", 9: "fall", 10: "fall", 11: "fall"}  # fmt: skip
HORIZON_BANDS = (("1-6", 1, 6), ("7-12", 7, 12), ("13-24", 13, 24))


@dataclass(frozen=True)
class Fold:
    k: int
    month: date  # first day of the EST month

    @property
    def start(self) -> datetime:
        return he_to_utc(self.month, 1)

    @property
    def end(self) -> datetime:
        nxt = (self.month.replace(day=28) + timedelta(days=4)).replace(day=1)
        return he_to_utc(nxt, 1)

    @property
    def train_end(self) -> datetime:
        return self.start - TRAIN_GAP


def default_folds(last_actual: datetime, n: int = 12) -> list[Fold]:
    """The n EST months ending with the last month whose test targets all have actuals."""
    month = est_date(last_actual - timedelta(hours=24)).replace(day=1)
    if Fold(0, month).end - timedelta(hours=TEST_STEP_H) + timedelta(hours=24) > last_actual:
        month = (month - timedelta(days=1)).replace(day=1)
    months = [month]
    for _ in range(n - 1):
        months.append((months[-1] - timedelta(days=1)).replace(day=1))
    return [Fold(k, m) for k, m in enumerate(reversed(months), start=1)]


def test_issue_times(fold: Fold, last_actual: datetime) -> pd.DatetimeIndex:
    idx = pd.date_range(fold.start, fold.end - timedelta(hours=1), freq="h", tz=UTC)
    idx = idx[idx.hour % TEST_STEP_H == 0]
    return idx[(idx + timedelta(hours=24)) <= pd.Timestamp(last_actual)]


def peak_thresholds(actuals: pd.Series) -> dict[date, float]:
    """EST date -> P90 of the 720 final actuals of the 30 EST days before it (NaN if incomplete)."""
    a = actuals.dropna()
    days: dict[date, list[float]] = {}
    for t, v in zip(pd.DatetimeIndex(a.index), a.to_numpy(np.float64), strict=True):
        days.setdefault(est_date(t.to_pydatetime()), []).append(float(v))
    out: dict[date, float] = {}
    if not days:
        return out
    d, last = min(days), max(days) + timedelta(days=1)
    while d <= last:
        ref = [days.get(d - timedelta(days=k), []) for k in range(1, 31)]
        complete = all(len(r) == 24 for r in ref)
        out[d] = float(np.percentile(np.concatenate(ref), 90)) if complete else float("nan")
        d += timedelta(days=1)
    return out


def label_peaks(targets: pd.Series, actual: pd.Series, thresholds: dict[date, float]) -> pd.Series:
    thr = np.array(
        [thresholds.get(est_date(t.to_pydatetime()), np.nan) for t in pd.DatetimeIndex(targets)]
    )
    y = actual.to_numpy(np.float64)
    out = pd.Series(y >= thr, index=targets.index, dtype="boolean")
    out[np.isnan(thr) | np.isnan(y)] = pd.NA
    return out


def score(df: pd.DataFrame) -> dict[str, float]:
    """Metrics on rows with p50, actual (and p10/p90, is_peak when present)."""
    d = df.dropna(subset=["p50", "actual"])
    y, p = d["actual"].to_numpy(), d["p50"].to_numpy()
    ape = np.abs(p - y) / y
    out: dict[str, float] = {
        "n": float(len(d)),
        "mape": float(ape.mean() * 100) if len(d) else np.nan,
        "rmse": float(np.sqrt(np.mean((p - y) ** 2))) if len(d) else np.nan,
        "pinball_50": float(np.mean(0.5 * np.abs(y - p))) if len(d) else np.nan,
    }
    if {"p10", "p90"} <= set(d.columns) and d["p10"].notna().any():
        lo, hi = d["p10"].to_numpy(), d["p90"].to_numpy()
        out["coverage_80"] = float(np.mean((y >= lo) & (y <= hi)))
        out["pinball_10"] = float(np.mean(np.maximum(0.1 * (y - lo), 0.9 * (lo - y))))
        out["pinball_90"] = float(np.mean(np.maximum(0.9 * (y - hi), 0.1 * (hi - y))))
        out["winkler_80"] = float(
            np.mean((hi - lo) + 10.0 * np.maximum(lo - y, 0) + 10.0 * np.maximum(y - hi, 0))
        )
    if "is_peak" in d.columns:
        pk = d["is_peak"].fillna(False).to_numpy(dtype=bool)
        out["n_peak"] = float(pk.sum())
        out["peak_mape"] = float(ape[pk].mean() * 100) if pk.any() else np.nan
    return out


@dataclass
class BacktestResult:
    folds: list[Fold]
    predictions: (
        pd.DataFrame
    )  # fold, issue_time, target_time, horizon, model_name, p10, p50, p90, actual, is_peak
    lag_parity: pd.DataFrame
    lgbm_offsets: IntervalOffsets | None = None
    fold_models: dict[int, Any] = field(default_factory=dict)  # fold -> LgbmForecaster

    def summary(self) -> pd.DataFrame:
        """One row per model: pooled / per-season / peak MAPE, coverage, skill vs sn168."""
        rows = []
        p = self.predictions
        for name, g in p.groupby("model_name", sort=False):
            row: dict[str, Any] = {"model": name, **score(g)}
            for season in ("winter", "spring", "summer", "fall"):
                row[f"mape_{season}"] = score(g[g["season"] == season])["mape"]
            for band, lo, hi in HORIZON_BANDS:
                row[f"mape_h{band}"] = score(g[g["horizon"].between(lo, hi)])["mape"]
            cov_rows = g[g["fold"].isin(HELDOUT_FOLDS)] if name == "lgbm" else g
            cov = score(cov_rows)
            row["coverage_80"] = cov.get("coverage_80", np.nan)
            row["coverage_basis"] = "folds 11-12 (held out)" if name == "lgbm" else "all folds"
            rows.append(row)
        out = pd.DataFrame(rows).set_index("model")
        if "seasonal_naive_168" in out.index:
            ref = float(out["mape"].to_dict()["seasonal_naive_168"])
            out["skill_vs_sn168"] = 1 - out["mape"] / ref
        return out

    def per_fold(self) -> pd.DataFrame:
        rows = [
            {"fold": k, "model": m, "mape": score(g)["mape"]}
            for (k, m), g in self.predictions.groupby(["fold", "model_name"])
        ]
        return pd.DataFrame(rows).pivot(index="fold", columns="model", values="mape")


def load_actuals(engine: Engine) -> pd.Series:
    """Final Ontario Demand by target_time (the `actuals` table: scoring truth, never a feature)."""
    with engine.connect() as conn:
        df = pd.read_sql(
            text("SELECT target_time, ontario_demand_mw::float8 AS v FROM actuals ORDER BY 1"), conn
        )
    idx = pd.DatetimeIndex(pd.to_datetime(df["target_time"], utc=True))
    return pd.Series(df["v"].to_numpy(np.float64), index=idx)


def _actual_lookup(actuals: pd.Series, targets_ns: np.ndarray) -> np.ndarray:
    a = pd.Series(actuals.to_numpy(np.float64), index=to_ns(actuals.index))
    return a.reindex(targets_ns).to_numpy()


def run_backtest(
    raw: pd.DataFrame,
    weather: pd.DataFrame,
    actuals: pd.Series,
    folds: Sequence[Fold] | None = None,
    with_lgbm: bool = True,
    log: Callable[[str], None] = print,
) -> BacktestResult:
    """Run the baselines (and LightGBM) over the folds; `actuals` = final Ontario Demand."""
    actuals = actuals.sort_index()
    last_actual = actuals.index.max().to_pydatetime()
    folds = list(folds or default_folds(last_actual))
    history = DemandHistory.from_raw(raw)
    fb = FeatureBuilder(history, weather)
    thresholds = peak_thresholds(actuals)

    test_sets = {f.k: test_issue_times(f, last_actual) for f in folds}
    test_all = from_ns(np.concatenate([to_ns(v) for v in test_sets.values()]))
    fold_of = pd.Series(
        np.concatenate([[k] * len(v) for k, v in test_sets.items()]), index=to_ns(test_all)
    )

    parts: list[pd.DataFrame] = []
    parity: list[dict[str, Any]] = []

    # ---- leakage assertion + lag parity on the test feature rows (h = 1), every fold
    probe = fb.frame(test_all, [1])  # assert_asof runs inside frame()
    for f in folds:
        rows = probe[probe["issue_time"].isin(test_sets[f.k])]
        issue_ns = to_ns(rows["issue_time"])
        l0_lag = (issue_ns - to_ns(rows["l0_target_time"].fillna(rows["issue_time"]))) / H_NS
        margin = (issue_ns + CUTOFF_NS - to_ns(rows["weather_available_at"])) / H_NS
        parity.append(
            {
                "fold": f.k,
                "month": f.month.isoformat(),
                "train_end": f.train_end.isoformat(),
                "n_test_issues": len(rows),
                "l0_lag_h": ",".join(
                    f"{k:g}:{v}" for k, v in pd.Series(l0_lag).value_counts().items()
                ),
                "weather_margin_h": f"{np.min(margin):g}..{np.max(margin):g}",
                "null_l0": int(rows["mkt_l0"].isna().sum()),
                "null_mean24": int(rows["mkt_mean24"].isna().sum()),
                "null_lag168": int(rows["ont_lag168"].isna().sum()),
            }
        )

    # ---- baselines on the test issues, intervals from each fold's trailing 60 days
    base = baseline_frame(history, to_ns(test_all))
    base["fold"] = fold_of.reindex(to_ns(base["issue_time"])).to_numpy()
    for f in folds:
        lo = f.train_end - BASELINE_INTERVAL_WINDOW
        win = pd.date_range(lo, f.train_end - timedelta(hours=25), freq="h", tz=UTC)
        hist = baseline_frame(history, to_ns(win))
        hist["actual"] = _actual_lookup(actuals, to_ns(hist["target_time"]))
        m = base["fold"] == f.k
        for name in BASELINES:
            off = fit_offsets(
                hist[hist["model_name"] == name], 1.0, f"trailing 60 d before {f.train_end}"
            )
            mm = m & (base["model_name"] == name)
            p10, p90 = off.apply(base.loc[mm, "p50"].to_numpy(), base.loc[mm, "horizon"].to_numpy())
            base.loc[mm, "p10"], base.loc[mm, "p90"] = p10, p90
    parts.append(base.drop(columns=["input_available_at"]))

    offsets: IntervalOffsets | None = None
    if with_lgbm:
        raise NotImplementedError("the LightGBM model arrives in C8")

    preds = pd.concat(parts, ignore_index=True)
    preds["actual"] = _actual_lookup(actuals, to_ns(preds["target_time"]))
    preds["is_peak"] = label_peaks(preds["target_time"], preds["actual"], thresholds)
    months = pd.DatetimeIndex(preds["target_time"]).tz_convert(EST).month
    preds["season"] = [SEASONS[m] for m in months]
    return BacktestResult(folds, preds, pd.DataFrame(parity), offsets)
