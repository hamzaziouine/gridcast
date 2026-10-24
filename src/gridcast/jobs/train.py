"""`gridcast train` (C9): backtest -> final fit -> object store -> MLflow -> registry -> bootstrap.

1. Rolling-origin backtest of LightGBM and the baselines (backtest/harness.py, §7.3).
2. Final fit of the 24 per-horizon boosters on every hour with a final actual.
3. Artifacts to the private bucket: `models/<version>/model.json.gz`, `intervals.v0001.json`
   (folds 1-10 residual quantiles x 1.15) and `backtest.json`.
4. MLflow run (DagsHub or local), `model_registry_events` rows `trained` and `backtested`.
5. Bootstrap (§4.7, ADR-0010) only when no `models/champion.json` exists yet:
   target met -> `bootstrap` / `ok`; else hard floor met -> `bootstrap-below-target` /
   `below_target`; else nothing is promoted (a bug under the slip rule), unless
   `allow_fallback` (after Fri 2026-10-30 12:00 UTC) promotes seasonal_naive_168 with
   `bootstrap-fallback` / `fallback`.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import numpy as np
import pandas as pd
from sqlalchemy import Engine, text

from gridcast.backtest.harness import (
    TRAIN_START,
    Fold,
    headline,
    headline_from_summary,
    load_actuals,
    run_backtest,
)
from gridcast.config import COVERAGE_BAND
from gridcast.features.build import (
    DemandHistory,
    FeatureBuilder,
    from_ns,
    load_history,
    load_weather,
    to_ns,
)
from gridcast.features.schema import FEATURE_SCHEMA_VERSION
from gridcast.models import lgbm as lgbm_mod
from gridcast.models.lgbm import LgbmForecaster
from gridcast.registry import mlflow_client
from gridcast.storage.objstore import ObjectStore

CHAMPION_KEY = "models/champion.json"
TARGET_MAPE = 4.0
TARGET_SKILL = 0.30
FLOOR_COVERAGE = (0.70, 0.90)


@dataclass(frozen=True)
class Decision:
    promote: str | None  # model_name to promote, or None
    reason: str | None
    status: str | None
    target_met: bool
    floor_met: bool


def decide_bootstrap(
    mape: float, sn168_mape: float, coverage: float, leakage_ok: bool, allow_fallback: bool
) -> Decision:
    """ADR-0010 bootstrap rule. Numbers are never relaxed to make a check pass (§12)."""
    skill = 1 - mape / sn168_mape
    target = (
        mape <= TARGET_MAPE
        and skill >= TARGET_SKILL
        and COVERAGE_BAND[0] <= coverage <= COVERAGE_BAND[1]
        and leakage_ok
    )
    floor = mape < sn168_mape and FLOOR_COVERAGE[0] <= coverage <= FLOOR_COVERAGE[1] and leakage_ok
    if target:
        return Decision("lgbm", "bootstrap", "ok", True, True)
    if floor:
        return Decision("lgbm", "bootstrap-below-target", "below_target", False, True)
    if allow_fallback:
        return Decision("seasonal_naive_168", "bootstrap-fallback", "fallback", False, False)
    return Decision(None, None, None, False, False)


def fit_final(
    raw: pd.DataFrame,
    weather: pd.DataFrame,
    actuals: pd.Series,
    log: Callable[[str], None],
    profile: str = "full",
) -> LgbmForecaster:
    """Fit the 24 boosters on every issue hour whose target has a final actual (the profile's
    issue grid)."""
    fb = FeatureBuilder(DemandHistory.from_raw(raw), weather)
    last = actuals.index.max()
    # Last issue: every target has an actual (target <= last) or is dropped, and weather covers
    # all 24 targets (the weather store may end before the actuals do).
    end = min(
        last - pd.Timedelta(hours=1),
        pd.Timestamp(weather["valid_time"].max()) - pd.Timedelta(hours=24),
    )
    issues = pd.date_range(TRAIN_START, end, freq="h", tz=UTC)
    issue_feats = fb.issue_frame(to_ns(issues))
    act = pd.Series(actuals.to_numpy(np.float64), index=to_ns(actuals.index))
    model = LgbmForecaster(profile=profile)
    for h in range(1, 25):
        fr = fb.frame(issues, [h], issue=issue_feats)
        y = act.reindex(to_ns(fr["target_time"])).to_numpy()
        ok = ~np.isnan(y) & lgbm_mod.keep_issue(to_ns(fr["issue_time"]), profile)
        model.fit_horizon(h, fr[ok], y[ok])
    log(f"final fit: {len(issues)} issue hours, last target {from_ns(to_ns([last]))[0]}")
    return model


def _md(df: pd.DataFrame) -> str:
    cols = [str(c) for c in df.columns]
    lines = ["| " + " | ".join([str(df.index.name or "")] + cols) + " |"]
    lines.append("|" + "---|" * (len(cols) + 1))
    for idx, row in df.iterrows():
        cells = [f"{v:.3f}" if isinstance(v, float) else str(v) for v in row.tolist()]
        lines.append("| " + " | ".join([str(idx)] + cells) + " |")
    return "\n".join(lines)


def _json_safe(x: Any) -> Any:
    """NaN/inf -> None (jsonb rejects them), numpy scalars -> Python, recursively."""
    if isinstance(x, dict):
        return {str(k): _json_safe(v) for k, v in x.items()}
    if isinstance(x, list | tuple):
        return [_json_safe(v) for v in x]
    if isinstance(x, float | np.floating):
        return float(x) if np.isfinite(x) else None
    if isinstance(x, np.integer):
        return int(x)
    return x


def _event(engine: Engine, **kw: Any) -> None:
    kw["metrics"] = json.dumps(_json_safe(kw.get("metrics")), allow_nan=False)
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO model_registry_events (event_type, model_name, model_version,"
                " mlflow_run_id, artifact_key, metrics, reason, triggered_by) VALUES (:event_type,"
                " :model_name, :model_version, :mlflow_run_id, :artifact_key,"
                " CAST(:metrics AS jsonb), :reason, :triggered_by)"
            ),
            kw,
        )


def run(
    engine: Engine,
    store: ObjectStore,
    allow_fallback: bool = False,
    log: Callable[[str], None] = print,
    now: datetime | None = None,
    folds: list[Fold] | None = None,
) -> dict[str, Any]:
    now = now or datetime.now(UTC)
    raw, weather, actuals = load_history(engine), load_weather(engine), load_actuals(engine)
    log(f"loaded raw={len(raw)} weather={len(weather)} actuals={len(actuals)}")
    result = run_backtest(
        raw, weather, actuals, folds=folds, log=log
    )  # raises LeakageError on any leak
    summary = result.summary()
    log(summary.round(3).T.to_string())
    nums = headline(result)
    model = fit_final(raw, weather, actuals, log)
    blob = model.to_bytes()
    sha = hashlib.sha256(blob).hexdigest()
    version = f"lgbm-{now:%Y.%m.%d}-{sha[:4]}"
    prefix = f"models/{version}/"
    artifact_key, intervals_key = prefix + "model.json.gz", prefix + "intervals.v0001.json"
    offsets = result.lgbm_offsets
    if offsets is None:  # pragma: no cover - run_backtest always fits LightGBM here
        raise RuntimeError("backtest produced no interval offsets")
    store.put_bytes(artifact_key, blob, "application/gzip")
    store.put_bytes(intervals_key, offsets.to_json().encode(), "application/json")
    backtest_doc = {
        "summary": json.loads(summary.to_json(orient="index")),
        "per_fold_mape": json.loads(result.per_fold().to_json(orient="index")),
        "lag_parity": json.loads(result.lag_parity.to_json(orient="records")),
        "folds": [f.month.isoformat() for f in result.folds],
    }
    store.put_json(prefix + "backtest.json", backtest_doc)

    with mlflow_client.start_run("gridcast-train", version, {"model_version": version}) as run:
        mlflow_client.log(
            {
                **lgbm_mod.PARAMS,
                "max_rounds": lgbm_mod.MAX_ROUNDS,
                "feature_schema_version": FEATURE_SCHEMA_VERSION,
                "artifact_key": artifact_key,
                "folds": ",".join(f.month.isoformat() for f in result.folds),
                "train_start": TRAIN_START.isoformat(),
            },
            nums,
            {
                "backtest_summary.md": _md(summary.round(4)),
                "backtest_per_fold_mape.md": _md(result.per_fold().round(3)),
                "lag_parity.md": _md(result.lag_parity.set_index("fold")),
                "intervals.json": offsets.to_json(),
            },
        )
        mlflow_run_id = run.info.run_id
    meta = {
        "model_version": version,
        "artifact_key": artifact_key,
        "intervals_key": intervals_key,
        "sha256": sha,
        "best_iterations": {str(h): n for h, n in model.best_iterations.items()},
        "mlflow_run_id": mlflow_run_id,
    }
    store.put_json(prefix + "train_meta.json", meta)
    return register(engine, store, version, allow_fallback, now)


def register(
    engine: Engine,
    store: ObjectStore,
    version: str,
    allow_fallback: bool = False,
    now: datetime | None = None,
    mlflow_run_id: str | None = None,
) -> dict[str, Any]:
    """Registry events + bootstrap for an artifact already in the bucket (idempotent).

    Runs at the end of `run`, and alone (`gridcast train --register-only`) when a run uploaded
    its artifacts but failed before writing to the database. The numbers come from the
    artifact's own `backtest.json`; the sha256 is recomputed from the stored artifact.
    """
    now = now or datetime.now(UTC)
    prefix = f"models/{version}/"
    artifact_key, intervals_key = prefix + "model.json.gz", prefix + "intervals.v0001.json"
    blob = store.get_bytes(artifact_key)
    sha = hashlib.sha256(blob).hexdigest()
    meta: dict[str, Any] = (
        store.get_json(prefix + "train_meta.json")
        if store.exists(prefix + "train_meta.json")
        else {}
    )
    if meta and meta["sha256"] != sha:
        raise ValueError(f"{artifact_key}: sha256 differs from train_meta.json")
    mlflow_run_id = mlflow_run_id or meta.get("mlflow_run_id")
    best = meta.get("best_iterations") or LgbmForecaster.from_bytes(blob).best_iterations
    doc = store.get_json(prefix + "backtest.json")
    nums = headline_from_summary(pd.DataFrame.from_dict(doc["summary"], orient="index"))
    common = {
        "model_name": "lgbm",
        "model_version": version,
        "mlflow_run_id": mlflow_run_id,
        "artifact_key": artifact_key,
        "triggered_by": "manual",
    }
    with engine.connect() as conn:
        seen = set(
            conn.execute(
                text("SELECT event_type FROM model_registry_events WHERE model_version = :v"),
                {"v": version},
            ).scalars()
        )
    if "trained" not in seen:
        _event(engine, event_type="trained", metrics={"best_iterations": best, "sha256": sha},
               reason="initial training (C9)", **common)  # fmt: skip
    if "backtested" not in seen:
        _event(engine, event_type="backtested", metrics=nums, reason="12-fold rolling origin",
               **common)  # fmt: skip

    out: dict[str, Any] = {"model_version": version, "artifact_key": artifact_key, **nums}
    if store.exists(CHAMPION_KEY):
        out["champion"] = "exists; not replaced (promotion goes through the gate)"
        return out
    d = decide_bootstrap(
        nums["lgbm_mape"],
        nums["sn168_mape"],
        nums["lgbm_coverage_80_heldout"],
        True,
        allow_fallback,
    )
    out.update(target_met=d.target_met, floor_met=d.floor_met)
    if d.promote is None:
        out["champion"] = "none: hard floor failed (bug under the slip rule); nothing promoted"
        return out
    metrics = {
        **nums,
        "kind": "bootstrap",
        "target": {"mape_max": TARGET_MAPE, "skill_min": TARGET_SKILL, "coverage": COVERAGE_BAND},
        "target_met": d.target_met,
    }
    if d.promote == "lgbm":
        champion = {
            "model_name": "lgbm",
            "model_version": version,
            "artifact_key": artifact_key,
            "sha256": sha,
            "feature_schema_version": FEATURE_SCHEMA_VERSION,
            "promoted_at": now.isoformat(),
            "mlflow_run_id": mlflow_run_id,
            "champion_status": d.status,
            "bootstrap_reason": d.reason,
            "intervals_key": intervals_key,
        }
    else:
        champion = {
            "model_name": "seasonal_naive_168",
            "model_version": "seasonal_naive_168",
            "artifact_key": None,
            "sha256": None,
            "feature_schema_version": FEATURE_SCHEMA_VERSION,
            "promoted_at": now.isoformat(),
            "mlflow_run_id": mlflow_run_id,
            "champion_status": d.status,
            "bootstrap_reason": d.reason,
            "intervals_key": None,
        }
    store.put_json(CHAMPION_KEY, champion)
    _event(
        engine,
        event_type="promoted",
        metrics=metrics,
        reason=str(d.reason),
        **{
            **common,
            "model_name": champion["model_name"],
            "model_version": champion["model_version"],
        },
    )
    out["champion"] = champion
    return out
