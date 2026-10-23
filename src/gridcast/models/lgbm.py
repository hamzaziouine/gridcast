"""LightGBM direct multi-horizon model: 24 per-horizon L1 regressors (C8, BUILD_PLAN §4.5).

Each horizon h has its own booster trained on rows (issue_time, h) with the v1 feature columns.
Early stopping uses the last 90 days of the training window; the booster is then refit on the
whole window with the best iteration count. The 24 boosters are saved together as one gzipped JSON
artifact holding LightGBM's text model format (no pickle: loading an artifact never runs code).
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


@dataclass
class LgbmForecaster:
    boosters: dict[int, lgb.Booster] = field(default_factory=dict)
    best_iterations: dict[int, int] = field(default_factory=dict)
    feature_schema_version: int = FEATURE_SCHEMA_VERSION

    def fit_horizon(self, h: int, frame: pd.DataFrame, y: np.ndarray) -> None:
        """Fit horizon h on `frame` (feature rows of that horizon, any order) and targets `y`."""
        x = frame[list(FEATURE_COLUMNS)].to_numpy(np.float64)
        issue = pd.DatetimeIndex(frame["issue_time"])
        val = issue >= issue.max() - EARLY_STOPPING_WINDOW
        best = MAX_ROUNDS
        if val.any() and not val.all():  # a window shorter than 90 days (tests) has no split
            dtrain = lgb.Dataset(
                x[~val], y[~val], feature_name=list(FEATURE_COLUMNS), free_raw_data=False
            )
            dval = lgb.Dataset(x[val], y[val], reference=dtrain)
            probe = lgb.train(
                PARAMS,
                dtrain,
                num_boost_round=MAX_ROUNDS,
                valid_sets=[dval],
                callbacks=[lgb.early_stopping(EARLY_STOPPING_ROUNDS, verbose=False)],
            )
            best = int(probe.best_iteration or MAX_ROUNDS)
        full = lgb.Dataset(x, y, feature_name=list(FEATURE_COLUMNS))
        self.boosters[h] = lgb.train(PARAMS, full, num_boost_round=best)
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
            "params": PARAMS,
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
        )
