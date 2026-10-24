"""C9 against real Postgres + moto S3: predict writes 24 x (champion + 3 baselines + IESO) rows."""

from __future__ import annotations

import hashlib
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta

import numpy as np
import pandas as pd
import pytest
from moto import mock_aws
from sqlalchemy import Engine, text

from gridcast import db
from gridcast.features.build import DemandHistory, FeatureBuilder, load_history, load_weather
from gridcast.jobs import predict
from gridcast.models import lgbm as lgbm_mod
from gridcast.models.intervals import IntervalOffsets
from gridcast.storage.objstore import ObjectStore, ObjstoreSettings, make_client, private_store
from tests.integration.test_features_db import END, START, load_synthetic

pytestmark = pytest.mark.integration

ISSUE = datetime(2024, 1, 25, 9, tzinfo=UTC)


@pytest.fixture(scope="module")
def engine(fresh_db_url: str) -> Engine:
    db.upgrade(fresh_db_url)
    eng = db.make_engine(fresh_db_url)
    load_synthetic(eng)
    with eng.begin() as conn:  # two IESO snapshots for the targets; only the first is <= cutoff
        for created, mw in (
            (ISSUE - timedelta(hours=3), 15000.0),
            (ISSUE + timedelta(hours=1), 1.0),
        ):
            for h in range(1, 25):
                t = ISSUE + timedelta(hours=h)
                conn.execute(
                    text(
                        "INSERT INTO ieso_forecast_snapshots (created_at, delivery_date,"
                        " target_time, energy_mw, version, source_file) VALUES (:c, :d, :t, :mw,"
                        " 1, 'f.xml')"
                    ),
                    {"c": created, "d": (t - timedelta(hours=5)).date(), "t": t, "mw": mw + h},
                )
    return eng


@pytest.fixture
def store(engine: Engine, monkeypatch: pytest.MonkeyPatch) -> Iterator[ObjectStore]:
    settings = ObjstoreSettings(
        _env_file=None,  # type: ignore[call-arg]
        region="us-east-1",
        access_key_id="test",
        secret_access_key="test",  # noqa: S106 - moto dummy
    )
    monkeypatch.setattr(lgbm_mod, "MAX_ROUNDS", 5)
    with mock_aws():
        make_client(settings).create_bucket(Bucket=settings.bucket_private)
        st = private_store(settings=settings)
        fb = FeatureBuilder(DemandHistory.from_raw(load_history(engine)), load_weather(engine))
        issues = pd.date_range(START + timedelta(days=2), END - timedelta(days=2), freq="h", tz=UTC)
        model = lgbm_mod.LgbmForecaster()
        for h in range(1, 25):
            fr = fb.frame(issues, [h])
            model.fit_horizon(h, fr, np.full(len(fr), 15000.0) + fr["mkt_l0"].to_numpy() * 0)
        blob = model.to_bytes()
        offsets = IntervalOffsets({h: -0.05 for h in range(1, 25)}, {h: 0.05 for h in range(1, 25)})
        st.put_bytes("models/v/model.json.gz", blob)
        st.put_bytes("models/v/intervals.v0001.json", offsets.to_json().encode())
        st.put_json(
            predict.CHAMPION_KEY,
            {
                "model_name": "lgbm",
                "model_version": "lgbm-test",
                "artifact_key": "models/v/model.json.gz",
                "sha256": hashlib.sha256(blob).hexdigest(),
                "intervals_key": "models/v/intervals.v0001.json",
                "champion_status": "ok",
            },
        )
        yield st


def test_predict_writes_all_model_rows_and_respects_insert_only(
    engine: Engine, store: ObjectStore
) -> None:
    out = predict.run(engine, store, ISSUE)
    assert out["rows_written"] == {
        "champion": 24,
        "ieso_adequacy": 24,
        "persistence": 24,
        "seasonal_naive_168": 24,
        "seasonal_naive_24": 24,
    }
    with engine.connect() as conn:
        rows = pd.read_sql(text("SELECT * FROM v_predictions ORDER BY model_name, horizon"), conn)
    assert len(rows) == 120
    ieso = rows[rows["model_name"] == "ieso_adequacy"]
    assert (ieso["p50"] == 15000 + ieso["horizon"]).all()  # the snapshot created before the cutoff
    assert (ieso["ieso_staleness_min"] == 210).all()  # 09:30Z cutoff - 06:00Z snapshot
    champ = rows[rows["model_name"] == "champion"]
    assert (champ["p10"] <= champ["p50"]).all() and (champ["p90"] >= champ["p50"]).all()
    assert set(rows["run_kind"]) == {"manual"}

    with engine.begin() as conn:  # a published champion row is never rewritten
        before = conn.execute(
            text(
                "UPDATE predictions SET p50 = p50 + 1 WHERE model_id = 1 AND horizon = 1"
                " RETURNING p50"
            )
        ).scalar_one()
    again = predict.run(engine, store, ISSUE, run_kind="catchup")
    assert again["rows_written"]["champion"] == 0
    assert again["rows_written"]["seasonal_naive_168"] == 24  # deterministic rows are upserted
    with engine.connect() as conn:
        p50 = conn.execute(
            text("SELECT p50 FROM predictions WHERE model_id = 1 AND horizon = 1")
        ).scalar_one()
    assert p50 == before
