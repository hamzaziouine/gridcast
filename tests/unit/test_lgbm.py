"""C8: LightGBM per-horizon model: fit, predict, artifact round trip, schema refusal."""

from __future__ import annotations

import gzip
import json
from datetime import UTC, datetime

import numpy as np
import pytest

from gridcast.features.build import DemandHistory, FeatureBuilder
from gridcast.models import lgbm as lgbm_mod
from gridcast.models.lgbm import LgbmForecaster
from tests.synthetic import raw_history, weather_frame

START = datetime(2024, 1, 1, tzinfo=UTC)
END = datetime(2024, 2, 15, tzinfo=UTC)


def test_fit_predict_and_round_trip(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(lgbm_mod, "MAX_ROUNDS", 20)
    raw = raw_history(START, END)
    fb = FeatureBuilder(DemandHistory.from_raw(raw), weather_frame(START, END))
    issues = [datetime(2024, 1, 20, tzinfo=UTC).replace(hour=h) for h in range(24)]
    frame = fb.frame(issues, [1, 2])
    y = raw.set_index("target_time")["ontario_demand_mw"].reindex(frame["target_time"]).to_numpy()
    model = LgbmForecaster()
    for h in (1, 2):
        m = frame["horizon"] == h
        model.fit_horizon(h, frame[m], y[m.to_numpy()])
    p = model.predict(frame)
    assert p.shape == (48,) and np.isfinite(p).all()
    blob = model.to_bytes()
    assert blob == model.to_bytes()  # deterministic artifact (gzip mtime=0)
    again = LgbmForecaster.from_bytes(blob)
    assert np.allclose(again.predict(frame), p)

    doc = json.loads(gzip.decompress(blob))
    doc["feature_schema_version"] = 99
    with pytest.raises(ValueError, match="feature schema"):
        LgbmForecaster.from_bytes(gzip.compress(json.dumps(doc).encode()))
