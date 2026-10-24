"""LightGBM direct multi-horizon model: 24 per-horizon L1 regressors (C8, BUILD_PLAN §4.5).

Each horizon h has its own booster trained on rows (issue_time, h) with the v1 feature columns.
The 24 boosters are saved together as one gzipped JSON artifact holding LightGBM's text model
format (no pickle: loading an artifact never runs code).

Two training profiles (ADR-0013):
- `full` (the bootstrap champion, C8): early stopping on the last 90 days of the training window,
  then a refit on the whole window with the best iteration count; hourly training issue grid.
- `fast` (retrain.yml on a 4-vCPU runner): no early-stopping probe, a fixed MAX_ROUNDS on the
  whole window (the champion's probes stopped at 341-400 of 400 rounds, 15 of 24 at >= 395),
  `max_bin` 63, and half the training issue hours (`keep_issue`: hour + day parity, so every
  horizon still sees every hour of the day).
"""

from __future__ import annotations

import gzip
import json
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any

import lightgbm as lgb
import numpy as np
import pandas as pd

from gridcast.features.schema import FEATURE_COLUMNS, FEATURE_SCHEMA_VERSION

EARLY_STOPPING_WINDOW = timedelta(days=90)
SEED = 20261028

PARAMS: dict[str, Any] = {
    "objective": "l1",
    "learning_rate": 0.03,
    "num_leaves": 63,
    "min_data_in_leaf": 50,
    "feature_fraction": 0.8,
    "bagging_fraction": 0.8,
    "bagging_freq": 1,
    "lambda_l2": 1.0,
    "seed": SEED,
    "deterministic": True,
    "force_row_wise": True,
    "num_threads": 0,
    "verbosity": -1,
}
MAX_ROUNDS = 400
EARLY_STOPPING_ROUNDS = 40
H_NS = 3_600_000_000_000


@dataclass(frozen=True)
class Profile:
    name: str
    params: dict[str, Any]  # overrides on PARAMS
    early_stopping: bool
    half_grid: bool  # train on half the issue hours (keep_issue)


PROFILES = {
    "full": Profile("full", {}, early_stopping=True, half_grid=False),
    "fast": Profile("fast", {"max_bin": 63}, early_stopping=False, half_grid=True),
}


def keep_issue(issue_ns: np.ndarray, profile: str) -> np.ndarray:
    """Training issue hours a profile keeps. `fast` keeps hour + day-number even: half the rows,
    alternating parity day by day so every horizon sees every target hour of the day."""
    if not PROFILES[profile].half_grid:
        return np.ones(len(issue_ns), dtype=bool)
    hours = np.asarray(issue_ns, dtype=np.int64) // H_NS
    return np.asarray((hours + hours // 24) % 2 == 0)


@dataclass
class LgbmForecaster:
    boosters: dict[int, lgb.Booster] = field(default_factory=dict)
    best_iterations: dict[int, int] = field(default_factory=dict)
    feature_schema_version: int = FEATURE_SCHEMA_VERSION
    profile: str = "full"

    @property
    def params(self) -> dict[str, Any]:
        return {**PARAMS, **PROFILES[self.profile].params}

    def fit_horizon(self, h: int, frame: pd.DataFrame, y: np.ndarray) -> None:
        """Fit horizon h on `frame` (feature rows of that horizon, any order) and targets `y`."""
        params = self.params
        x = frame[list(FEATURE_COLUMNS)].to_numpy(np.float64)
        issue = pd.DatetimeIndex(frame["issue_time"])
        val = issue >= issue.max() - EARLY_STOPPING_WINDOW
        best = MAX_ROUNDS
        # a window shorter than 90 days (tests) has no split
        if PROFILES[self.profile].early_stopping and val.any() and not val.all():
            dtrain = lgb.Dataset(
                x[~val],
                y[~val],
                feature_name=list(FEATURE_COLUMNS),
                params=params,
                free_raw_data=False,
            )
            dval = lgb.Dataset(x[val], y[val], reference=dtrain)
            probe = lgb.train(
                params,
                dtrain,
                num_boost_round=MAX_ROUNDS,
                valid_sets=[dval],
                callbacks=[lgb.early_stopping(EARLY_STOPPING_ROUNDS, verbose=False)],
            )
            best = int(probe.best_iteration or MAX_ROUNDS)
        full = lgb.Dataset(x, y, feature_name=list(FEATURE_COLUMNS), params=params)
        self.boosters[h] = lgb.train(params, full, num_boost_round=best)
        self.best_iterations[h] = best

    def predict(self, frame: pd.DataFrame) -> np.ndarray:
        """p50 for each row of a feature frame (rows may mix horizons)."""
        out = np.full(len(frame), np.nan)
        hz = frame["horizon"].to_numpy().astype(int)
        x = frame[list(FEATURE_COLUMNS)].to_numpy(np.float64)
        for h in np.unique(hz):
            m = hz == h
            out[m] = self.boosters[int(h)].predict(x[m])
        return out

    def to_bytes(self) -> bytes:
        doc = {
            "format": "gridcast-lgbm-v1",
            "feature_schema_version": self.feature_schema_version,
            "feature_columns": list(FEATURE_COLUMNS),
            "profile": self.profile,
            "params": self.params,
            "best_iterations": {str(h): n for h, n in sorted(self.best_iterations.items())},
            "boosters": {str(h): b.model_to_string() for h, b in sorted(self.boosters.items())},
        }
        return gzip.compress(json.dumps(doc).encode("utf-8"), mtime=0)

    @classmethod
    def from_bytes(cls, raw: bytes) -> LgbmForecaster:
        doc = json.loads(gzip.decompress(raw))
        if doc.get("format") != "gridcast-lgbm-v1":
            raise ValueError("not a gridcast LightGBM artifact")
        if doc["feature_schema_version"] != FEATURE_SCHEMA_VERSION or doc[
            "feature_columns"
        ] != list(FEATURE_COLUMNS):
            raise ValueError(
                f"artifact feature schema v{doc['feature_schema_version']} != code "
                f"v{FEATURE_SCHEMA_VERSION}"
            )
        return cls(
            boosters={int(h): lgb.Booster(model_str=s) for h, s in doc["boosters"].items()},
            best_iterations={int(h): int(n) for h, n in doc["best_iterations"].items()},
            feature_schema_version=int(doc["feature_schema_version"]),
            profile=str(doc.get("profile", "full")),  # artifacts before ADR-0013 are `full`
        )
