"""C18 against real Postgres + moto: `retrain.run` registers a candidate and never promotes it."""

from __future__ import annotations

import json
from collections.abc import Iterator
from datetime import UTC, date, datetime, timedelta
from typing import Any

import pytest
from moto import mock_aws
from sqlalchemy import Engine, text

from gridcast import db
from gridcast.backtest import harness
from gridcast.features.build import load_history, load_weather
from gridcast.ingest.http import Fetched
from gridcast.jobs import daily, predict, retrain, train
from gridcast.models import lgbm as lgbm_mod
from tests.integration.helpers import install_champion, make_buckets
from tests.integration.test_features_db import load_synthetic

pytestmark = pytest.mark.integration

START = datetime(2024, 1, 1, tzinfo=UTC)
END = datetime(2024, 4, 1, tzinfo=UTC)
NOW = datetime(2024, 4, 2, 3, tzinfo=UTC)
FOLDS = [harness.Fold(11, date(2024, 2, 1)), harness.Fold(12, date(2024, 3, 1))]


def weather_down(url: str, params: dict[str, str]) -> Fetched:
    return Fetched(url, 503, "", NOW, None)


@pytest.fixture(scope="module")
def env(fresh_db_url: str, tmp_path_factory: pytest.TempPathFactory) -> Iterator[Any]:
    db.upgrade(fresh_db_url)
    eng = db.make_engine(fresh_db_url)
    load_synthetic(eng, START, END)
    with eng.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO actuals (target_time, ontario_demand_mw, available_at,"
                " available_at_kind) SELECT target_time, ontario_demand_mw, available_at,"
                " available_at_kind FROM raw_demand_history"
            )
        )
    mlruns = tmp_path_factory.mktemp("mlruns")
    with pytest.MonkeyPatch.context() as mp, mock_aws():
        mp.setattr(lgbm_mod, "MAX_ROUNDS", 5)
        mp.setattr(harness, "TRAIN_START", datetime(2024, 1, 2, tzinfo=UTC))
        mp.setattr(train, "TRAIN_START", datetime(2024, 1, 2, tzinfo=UTC))
        mp.setenv("MLFLOW_TRACKING_URI", f"sqlite:///{mlruns}/mlflow.db".replace("\\", "/"))
        mp.delenv("HEALTHCHECKS_RETRAIN_URL", raising=False)
        private, _ = make_buckets()
        # an lgbm champion trained `full` whose recorded backtest is the full procedure on FOLDS
        res = harness.run_backtest(
            load_history(eng), load_weather(eng), harness.load_actuals(eng), folds=FOLDS,
            log=lambda _: None,
        )  # fmt: skip
        private.put_json(
            "models/lgbm-test/backtest.json",
            {
                "summary": json.loads(res.summary().to_json(orient="index")),
                "folds": [f.month.isoformat() for f in FOLDS],
            },
        )
        install_champion(eng, private, START, END)
        yield eng, private, mp
    eng.dispose()


def _retrain(env: Any, profile: str) -> dict[str, Any]:
    eng, store, _ = env
    deps = retrain.Deps(fetch_weather=weather_down, now=lambda: NOW)
    return retrain.run(eng, store, deps, "manual", profile, "manual", log=lambda _: None)


def _events(engine: Engine) -> list[str]:
    with engine.connect() as conn:
        return [r[0] for r in conn.execute(text("SELECT event_type FROM model_registry_events"
                                                " ORDER BY event_id"))]  # fmt: skip


def test_full_profile_is_a_refit_that_reproduces_the_champion_backtest(env: Any) -> None:
    eng, store, _ = env
    champion = store.get_json(predict.CHAMPION_KEY)
    out = _retrain(env, "full")
    assert out["status"] == "ok", out
    assert any(n.startswith("weather refresh") for n in out["notes"])  # outage = note
    assert out["kind"] == "refit"
    assert out["gate"]["why"].startswith("refit determinism")
    assert out["mape"] == pytest.approx(
        out["reference_mape"], rel=1e-9
    )  # same procedure (JSON keeps 10 digits)
    assert store.get_json(predict.CHAMPION_KEY) == champion  # a retrain never promotes
    assert _events(eng)[-3:-1] == ["trained", "backtested"]
    meta = store.get_json(f"models/{out['model_version']}/train_meta.json")
    assert meta["profile"] == "full" and meta["kind"] == "refit"


def test_fast_candidate_is_a_challenger_and_slots_follow_the_queue_rules(env: Any) -> None:
    eng, store, mp = env
    champion = store.get_json(predict.CHAMPION_KEY)
    real = retrain.backtest_stage
    mp.setattr(retrain, "backtest_stage", lambda *a: retrain.StageResult(False, "forced fail"))
    out = _retrain(env, "fast")
    assert out["kind"] == "challenger" and out["decision"] == "rejected"
    assert not store.exists(predict.SHADOW_KEY)
    mp.setattr(retrain, "backtest_stage", lambda *a: retrain.StageResult(True, "forced pass"))
    first = _retrain(env, "fast")
    assert first["decision"] == "shadow_started"
    shadow = store.get_json(predict.SHADOW_KEY)
    assert shadow["kind"] == "challenger" and shadow["profile"] == "fast"
    assert shadow["started_at"] == NOW.isoformat()
    second = _retrain(env, "fast")
    assert second["decision"] == "candidate_queued"  # a running shadow is never replaced
    assert store.get_json(predict.SHADOW_KEY) == shadow
    assert store.get_json(retrain.QUEUED_KEY)["model_version"] == second["model_version"]
    mp.setattr(retrain, "backtest_stage", real)
    assert store.get_json(predict.CHAMPION_KEY) == champion
    assert _events(eng)[-3:] == ["trained", "backtested", "candidate_queued"]
    assert "rejected" in _events(eng) and "shadow_started" in _events(eng)


def test_shadow_rows_are_written_from_its_start_only(env: Any) -> None:
    eng, store, _ = env
    shadow = store.get_json(predict.SHADOW_KEY)  # left by the previous test, started at NOW
    store.put_json(predict.SHADOW_KEY, {**shadow, "started_at": "2024-03-10T12:30:00+00:00"})
    before = predict.run(eng, store, datetime(2024, 3, 10, 11, tzinfo=UTC))
    assert before["rows_written"].get("shadow", 0) == 0 and before["shadow_error"] is None
    after = predict.run(eng, store, datetime(2024, 3, 10, 12, tzinfo=UTC))
    assert after["rows_written"]["shadow"] == 24 and after["rows_written"]["champion"] == 24
    store.put_json(
        predict.SHADOW_KEY,
        {
            **shadow,
            "started_at": "2024-03-10T12:30:00+00:00",
            "artifact_key": "models/missing.json.gz",
            "sha256": "0" * 64,
        },
    )
    broken = predict.run(eng, store, datetime(2024, 3, 11, 12, tzinfo=UTC))
    assert broken["rows_written"]["champion"] == 24  # a broken shadow never costs the champion
    assert broken["shadow_error"] == "NoSuchKey"


def test_drift_trigger_needs_3_days_7_live_days_and_respects_the_cooldown(env: Any) -> None:
    eng = env[0]

    def rows(breached: bool, days: int, live: int) -> list[dict[str, Any]]:
        ctx = {"consecutive_days": days, "live_days": live}
        return [
            {"metric": "rolling_mape_7d", "value": 4.0, "br": breached, "ctx": json.dumps(ctx)},
            {"metric": "ref_mape_30d", "value": 2.6, "br": False, "ctx": json.dumps(ctx)},
        ]

    later = datetime.now(UTC) + timedelta(days=4)  # past the 72 h cooldown of the runs above
    assert daily.retrain_trigger(eng, rows(True, 3, 7), later) == "drift:mape"
    assert daily.retrain_trigger(eng, rows(True, 2, 7), later) is None
    assert daily.retrain_trigger(eng, rows(True, 3, 6), later) is None  # not a 7-day MAPE yet
    assert daily.retrain_trigger(eng, rows(False, 0, 30), later) is None
    assert daily.retrain_trigger(eng, rows(True, 5, 30), datetime.now(UTC)) is None  # cooldown
