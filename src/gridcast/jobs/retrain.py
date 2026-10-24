"""`gridcast run retrain` (C18, BUILD_PLAN §6.3 and §4.7 steps 1 and 6, ADR-0013).

1. Refresh `weather_hourly` (Open-Meteo Historical Forecast API) for the recent months up to
   yesterday. An outage is a note: training uses what the store has.
2. Load demand history, weather and final actuals (Neon; one read per retrain).
3. Rolling-origin backtest of the candidate on **the champion's own folds** (the fold list in its
   `backtest.json`), so the candidate's numbers compare like for like with the champion's
   recorded backtest without refitting the champion procedure on the runner (§4.7: "both
   procedures refit on identical folds"; the champion's per-fold run is deterministic, §16).
4. Final fit on every hour with a final actual (profile's issue grid), artifacts to
   `models/<version>/` (model, `intervals.v0001.json`, `backtest.json`, `train_meta.json`,
   `reference_features.parquet` for the PSI diagnostic).
5. MLflow run (experiment `gridcast-retrain`; DagsHub when MLFLOW_TRACKING_URI is set). An
   MLflow outage is a note: serving never depends on it (ADR-0005).
6. Registry events `trained` and `backtested`, then the backtest stage of the gate (§4.7 step 1):
   - kind: `refit` when the candidate's training profile equals the champion's, else
     `challenger` (a different procedure is a challenger by the plan's definition);
   - refit: pooled MAPE within ±0.5 % relative of the champion's recorded backtest MAPE;
   - challenger: MAPE ≤ 0.98 × the champion's (≥ 2 % relative gain);
   - both: held-out `coverage_80` in COVERAGE_BAND.
   Pass -> `shadow.json` + `shadow_started` when no shadow runs; a challenger may pre-empt a refit
   shadow; otherwise `queued.json` + `candidate_queued` (one slot, newest wins). Fail ->
   `rejected` with every number in `metrics`.

Nothing here touches `champion.json`: promotion happens only in the gate after ≥ 14 days of
shadow (§4.7 step 2), never in a retrain.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from typing import Any

from sqlalchemy import Engine

from gridcast.backtest.harness import TRAIN_START, Fold, headline, load_actuals, run_backtest
from gridcast.config import COVERAGE_BAND, get_settings
from gridcast.drift import psi as psi_mod
from gridcast.features.build import load_history, load_weather
from gridcast.features.schema import FEATURE_SCHEMA_VERSION
from gridcast.ingest import openmeteo
from gridcast.jobs import runs
from gridcast.jobs.predict import CHAMPION_KEY, SHADOW_KEY
from gridcast.jobs.train import _event, _md, fit_final
from gridcast.models import lgbm as lgbm_mod
from gridcast.registry import mlflow_client
from gridcast.storage.objstore import ObjectStore
from gridcast.timeutil import floor_hour

JOB = "retrain"
QUEUED_KEY = "models/queued.json"
REFIT_TOLERANCE = 0.005  # §4.7 step 1, refit determinism check
CHALLENGER_RATIO = 0.98  # §4.7 step 1, ≥ 2 % relative backtest gain
WEATHER_REFRESH_DAYS = 40


@dataclass
class Deps:
    fetch_weather: openmeteo.Fetch  # Historical Forecast API
    now: Callable[[], datetime] = field(default=lambda: datetime.now(UTC))
    ping: runs.Pinger = runs.http_get


@dataclass(frozen=True)
class StageResult:
    passed: bool
    why: str


def backtest_stage(kind: str, mape: float, ref_mape: float, coverage: float) -> StageResult:
    """§4.7 step 1. `ref_mape` = the champion's recorded backtest MAPE on the same folds."""
    rel = mape / ref_mape - 1
    if kind == "refit":
        passed = abs(rel) <= REFIT_TOLERANCE
        why = (
            f"refit determinism: MAPE {mape:.3f} vs champion {ref_mape:.3f} ({rel:+.2%};"
            f" tolerance ±{REFIT_TOLERANCE:.1%})"
        )
    elif kind == "challenger":
        passed = mape <= CHALLENGER_RATIO * ref_mape
        why = (
            f"challenger: MAPE {mape:.3f} vs {CHALLENGER_RATIO} x champion {ref_mape:.3f}"
            f" = {CHALLENGER_RATIO * ref_mape:.3f} ({rel:+.2%})"
        )
    else:
        raise ValueError(f"unknown kind {kind!r}")
    in_band = COVERAGE_BAND[0] <= coverage <= COVERAGE_BAND[1]
    why += f"; held-out coverage_80 {coverage:.3f} {'in' if in_band else 'outside'} {COVERAGE_BAND}"
    return StageResult(passed and in_band, why)


def slot_action(shadow: dict[str, Any] | None, kind: str) -> str:
    """§4.7 step 6: 'start' (no shadow), 'preempt' (a challenger replaces a refit shadow) or
    'queue' (a shadow is running: one queue slot, newest wins)."""
    if shadow is None:
        return "start"
    if kind == "challenger" and shadow.get("kind") == "refit":
        return "preempt"
    return "queue"


def champion_profile(champion: dict[str, Any]) -> str:
    """Pointers written before ADR-0013 carry no profile: that champion was trained `full`."""
    return str(champion.get("profile") or "full")


def refresh_weather(
    engine: Engine, deps: Deps, today: date, notes: runs.Notes, log: Callable[[str], None]
) -> int:
    start = (today - timedelta(days=WEATHER_REFRESH_DAYS)).replace(day=1)
    try:
        return openmeteo.backfill(
            engine, deps.fetch_weather, start, today - timedelta(days=1), log=log
        )
    except Exception as exc:
        notes.error("weather refresh", exc)
        return 0


def run(
    engine: Engine,
    store: ObjectStore,
    deps: Deps,
    reason: str = "manual",
    profile: str = "fast",
    run_kind: str = "manual",
    log: Callable[[str], None] = print,
) -> dict[str, Any]:
    if profile not in lgbm_mod.PROFILES:
        raise ValueError(f"unknown profile {profile!r}")
    now = deps.now()
    hc = get_settings().healthchecks_retrain_url
    run_id = runs.start(engine, JOB, floor_hour(now), run_kind)
    notes = runs.Notes()
    out: dict[str, Any] = {"reason": reason, "profile": profile, "run_kind": run_kind}
    try:
        trigger = "drift" if reason.startswith("drift") else run_kind
        out.update(_retrain(engine, store, deps, reason, profile, trigger, now, notes, log))
        status = "ok"
    except Exception as exc:
        notes.error("retrain", exc)
        status = "failed"
    out["status"] = runs.finish(engine, run_id, status, 0, notes.private)
    out["notes"] = notes.public
    out["heartbeat"] = runs.heartbeat(hc, out["status"] != "failed", deps.ping)
    return out


def _retrain(
    engine: Engine,
    store: ObjectStore,
    deps: Deps,
    reason: str,
    profile: str,
    triggered_by: str,
    now: datetime,
    notes: runs.Notes,
    log: Callable[[str], None],
) -> dict[str, Any]:
    out: dict[str, Any] = {}
    out["weather_rows"] = refresh_weather(engine, deps, now.date(), notes, log)

    champion = store.get_json(CHAMPION_KEY)
    if champion.get("model_name") != "lgbm":
        raise ValueError("champion is not a LightGBM model: no recorded backtest to compare with")
    champ_bt = store.get_json(f"models/{champion['model_version']}/backtest.json")
    # numbered so the last two are the held-out folds 11-12 (harness.HELDOUT_FOLDS)
    months = champ_bt["folds"]
    folds = [Fold(k, date.fromisoformat(m)) for k, m in enumerate(months, 13 - len(months))]
    ref = champ_bt["summary"]["lgbm"]
    ref_mape = float(ref["mape"])
    kind = "refit" if champion_profile(champion) == profile else "challenger"
    out.update(kind=kind, champion=champion["model_version"], reference_mape=ref_mape)

    raw, weather, actuals = load_history(engine), load_weather(engine), load_actuals(engine)
    log(f"loaded raw={len(raw)} weather={len(weather)} actuals={len(actuals)}")
    result = run_backtest(raw, weather, actuals, folds=folds, log=log, profile=profile)
    nums = headline(result)
    model = fit_final(raw, weather, actuals, log, profile=profile)
    blob = model.to_bytes()
    sha = hashlib.sha256(blob).hexdigest()
    version = f"lgbm-{now:%Y.%m.%d}-{sha[:4]}"
    prefix = f"models/{version}/"
    artifact_key, intervals_key = prefix + "model.json.gz", prefix + "intervals.v0001.json"
    offsets = result.lgbm_offsets
    if offsets is None:  # pragma: no cover - run_backtest always fits LightGBM here
        raise RuntimeError("backtest produced no interval offsets")
    summary = result.summary()
    store.put_bytes(artifact_key, blob, "application/gzip")
    store.put_bytes(
        psi_mod.reference_key(version),
        psi_mod.to_parquet(psi_mod.build_reference(raw, weather, actuals)),
        "application/vnd.apache.parquet",
    )
    store.put_bytes(intervals_key, offsets.to_json().encode(), "application/json")
    store.put_json(
        prefix + "backtest.json",
        {
            "summary": json.loads(summary.to_json(orient="index")),
            "per_fold_mape": json.loads(result.per_fold().to_json(orient="index")),
            "lag_parity": json.loads(result.lag_parity.to_json(orient="records")),
            "folds": [f.month.isoformat() for f in result.folds],
            "profile": profile,
        },
    )

    stage = backtest_stage(kind, nums["lgbm_mape"], ref_mape, nums["lgbm_coverage_80_heldout"])
    gate = {
        "kind": kind,
        "profile": profile,
        "champion_version": champion["model_version"],
        "champion_profile": champion_profile(champion),
        "reference_mape": ref_mape,
        "reference_coverage_80": ref.get("coverage_80"),
        "passed": stage.passed,
        "why": stage.why,
    }
    mlflow_run_id: str | None = None
    try:
        with mlflow_client.start_run(
            "gridcast-retrain",
            version,
            {"model_version": version, "kind": kind, "profile": profile, "reason": reason},
        ) as mlrun:
            mlflow_client.log(
                {
                    **model.params,
                    "max_rounds": lgbm_mod.MAX_ROUNDS,
                    "profile": profile,
                    "kind": kind,
                    "reason": reason,
                    "feature_schema_version": FEATURE_SCHEMA_VERSION,
                    "artifact_key": artifact_key,
                    "folds": ",".join(f.month.isoformat() for f in result.folds),
                    "train_start": TRAIN_START.isoformat(),
                    "champion_version": champion["model_version"],
                },
                {**nums, "reference_mape": ref_mape},
                {
                    "backtest_summary.md": _md(summary.round(4)),
                    "backtest_per_fold_mape.md": _md(result.per_fold().round(3)),
                    "gate_backtest_stage.json": json.dumps(gate, indent=1),
                    "intervals.json": offsets.to_json(),
                },
            )
            mlflow_run_id = mlrun.info.run_id
    except Exception as exc:
        notes.error("mlflow", exc)
    pointer: dict[str, Any] = {
        "model_name": "lgbm",
        "model_version": version,
        "artifact_key": artifact_key,
        "sha256": sha,
        "feature_schema_version": FEATURE_SCHEMA_VERSION,
        "intervals_key": intervals_key,
        "mlflow_run_id": mlflow_run_id,
        "profile": profile,
        "kind": kind,
        "reason": reason,
        "registered_at": now.isoformat(),
        "backtest": {"mape": nums["lgbm_mape"], "coverage_80": nums["lgbm_coverage_80_heldout"]},
    }
    store.put_json(prefix + "train_meta.json", {**pointer, "gate": gate})
    common = {
        "model_name": "lgbm",
        "model_version": version,
        "mlflow_run_id": mlflow_run_id,
        "artifact_key": artifact_key,
        "triggered_by": triggered_by,
    }
    _event(engine, event_type="trained", reason=reason, **common,
           metrics={"best_iterations": model.best_iterations, "sha256": sha, "profile": profile,
                    "kind": kind})  # fmt: skip
    _event(engine, event_type="backtested", reason=stage.why, **common,
           metrics={**nums, **gate})  # fmt: skip
    out.update(model_version=version, mape=nums["lgbm_mape"], gate=gate)
    if not stage.passed:
        _event(engine, event_type="rejected", reason=f"backtest stage: {stage.why}", **common,
               metrics={**nums, **gate})  # fmt: skip
        out["decision"] = "rejected"
        return out

    shadow = store.get_json(SHADOW_KEY) if store.exists(SHADOW_KEY) else None
    action = slot_action(shadow, kind)
    if action in ("start", "preempt"):
        store.put_json(SHADOW_KEY, {**pointer, "started_at": now.isoformat()})
        why = "no shadow running" if action == "start" else "challenger pre-empts a refit shadow"
        _event(engine, event_type="shadow_started", reason=why, **common,
               metrics={**gate, "replaced": (shadow or {}).get("model_version")})  # fmt: skip
        out["decision"] = "shadow_started"
    else:
        store.put_json(QUEUED_KEY, pointer)
        _event(engine, event_type="candidate_queued", reason="a shadow is running", **common,
               metrics={**gate, "shadow": (shadow or {}).get("model_version")})  # fmt: skip
        out["decision"] = "candidate_queued"
    return out
