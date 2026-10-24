"""C9 against real Postgres + moto S3: `train.run` end to end on a small synthetic history."""

from __future__ import annotations

from datetime import UTC, date, datetime

import pytest
from moto import mock_aws
from sqlalchemy import Engine, text

from gridcast import db
from gridcast.backtest import harness
from gridcast.jobs import train
from gridcast.models import lgbm as lgbm_mod
from gridcast.models.lgbm import LgbmForecaster
from gridcast.storage.objstore import ObjstoreSettings, make_client, private_store
from tests.integration.test_features_db import load_synthetic

pytestmark = pytest.mark.integration

START = datetime(2024, 1, 1, tzinfo=UTC)
END = datetime(2024, 4, 1, tzinfo=UTC)


@pytest.fixture(scope="module")
def engine(fresh_db_url: str) -> Engine:
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
    return eng


def test_train_bootstraps_champion_and_logs_events(
    engine: Engine, monkeypatch: pytest.MonkeyPatch, tmp_path: object
) -> None:
    monkeypatch.setattr(lgbm_mod, "MAX_ROUNDS", 5)
    monkeypatch.setattr(harness, "TRAIN_START", datetime(2024, 1, 2, tzinfo=UTC))
    monkeypatch.setattr(train, "TRAIN_START", datetime(2024, 1, 2, tzinfo=UTC))
    monkeypatch.setenv("MLFLOW_TRACKING_URI", f"sqlite:///{tmp_path}/mlflow.db".replace("\\", "/"))
    settings = ObjstoreSettings(
        _env_file=None,  # type: ignore[call-arg]
        region="us-east-1",
        access_key_id="test",
        secret_access_key="test",  # noqa: S106 - moto dummy
    )
    folds = [harness.Fold(1, date(2024, 2, 1)), harness.Fold(11, date(2024, 3, 1))]
    with mock_aws():
        make_client(settings).create_bucket(Bucket=settings.bucket_private)
        store = private_store(settings=settings)
        # 5-round boosters on 3 synthetic months need not beat sn168: allow the fallback branch
        out = train.run(engine, store, allow_fallback=True, log=lambda _: None, folds=folds)
        assert store.exists(out["artifact_key"])
        champ = store.get_json(train.CHAMPION_KEY)
        model = LgbmForecaster.from_bytes(store.get_bytes(out["artifact_key"]))
        assert sorted(model.boosters) == list(range(1, 25))
        again = train.run(engine, store, log=lambda _: None, folds=folds)
        assert again["model_version"] == out["model_version"]  # deterministic artifact
        reg = train.register(engine, store, out["model_version"])  # resume path, idempotent
    expected = {
        (True, True): "bootstrap",
        (False, True): "bootstrap-below-target",
        (False, False): "bootstrap-fallback",
    }[(out["target_met"], out["floor_met"])]
    assert champ["bootstrap_reason"] == expected
    assert champ["champion_status"] in {"ok", "below_target", "fallback"}
    assert again["champion"].startswith("exists") and reg["champion"].startswith("exists")
    with engine.connect() as conn:
        events = conn.execute(
            text("SELECT event_type, reason FROM model_registry_events ORDER BY event_id")
        ).all()
    kinds = [e[0] for e in events]
    assert kinds == ["trained", "backtested", "promoted"]  # re-runs add no duplicate events
    assert events[2][1] == expected
