"""Feature drift (PSI, l0_lag_parity) and the PSI+MAPE trigger against real Postgres + moto."""

from __future__ import annotations

import json
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import Any

import numpy as np
import pandas as pd
import pytest
from moto import mock_aws
from sqlalchemy import Engine

from gridcast import db
from gridcast.drift import checks, psi
from gridcast.jobs import daily
from gridcast.storage.objstore import ObjectStore
from tests.integration.helpers import make_buckets

pytestmark = pytest.mark.integration

HI = datetime(2026, 1, 20, 5, tzinfo=UTC)  # 2026-01-20 00:00 EST
CHAMPION = {"model_name": "lgbm", "model_version": "lgbm-x"}


@pytest.fixture(scope="module")
def env(fresh_db_url: str) -> Iterator[tuple[Engine, ObjectStore]]:
    db.upgrade(fresh_db_url)
    eng = db.make_engine(fresh_db_url)
    rng = np.random.default_rng(7)
    issues = pd.date_range(HI - timedelta(days=14), HI - timedelta(hours=1), freq="h")
    live = pd.DataFrame({"issue_time": issues, "mkt_l0_age_h": 1.0})
    for f in psi.PSI_FEATURES:
        live[f] = rng.normal(0, 1, len(issues))
    live["temperature_2m_w"] += 2.0  # a warm spell: shifted by 2 sd
    live["dew_point_2m_w"] += 2.0
    live.loc[live.index[-3:], "mkt_l0_age_h"] = 3.0  # three fallback hours at the end
    with eng.begin() as conn:
        live.to_sql("live_features", conn, if_exists="append", index=False)
    ref_t = pd.date_range(datetime(2019, 1, 1, tzinfo=UTC), datetime(2025, 12, 31, tzinfo=UTC),
                          freq="h")  # fmt: skip
    ref = pd.DataFrame({"target_time": ref_t})
    for f in psi.PSI_FEATURES:
        ref[f] = rng.normal(0, 1, len(ref_t))
    with mock_aws():
        private, _ = make_buckets()
        private.put_bytes(psi.reference_key("lgbm-x"), psi.to_parquet(ref))
        yield eng, private
    eng.dispose()


def test_psi_flags_only_the_shifted_features_and_parity_reads_the_live_lag(env: Any) -> None:
    eng, store = env
    rows, notes = checks.feature_drift(eng, store, CHAMPION, HI, HI)
    assert notes == []
    by = {(r["metric"], r["feature"]): r for r in rows}
    hot = {f for (m, f), r in by.items() if m == "psi" and r["br"]}
    assert hot == {"temperature_2m_w", "dew_point_2m_w"}
    assert len([k for k in by if k[0] == "psi"]) == len(psi.PSI_FEATURES)
    par = by[("l0_lag_parity", "")]
    assert par["value"] == pytest.approx((21 * 1.0 + 3 * 3.0) / 24)
    assert not par["br"]  # mean 1.25 h vs training 1 h: within 1 h
    assert json.loads(par["ctx"])["share_fallback"] == pytest.approx(3 / 24, abs=1e-4)
    daily.write_drift(eng, rows)
    again, _ = checks.feature_drift(eng, store, CHAMPION, HI, HI + timedelta(days=1))
    streak = {r["feature"]: json.loads(r["ctx"])["consecutive_days"] for r in again}
    assert streak["temperature_2m_w"] == 2 and streak["wind_speed_10m_w"] == 0


def test_psi_waits_for_7_days_and_a_reference(env: Any) -> None:
    eng, store = env
    rows, notes = checks.feature_drift(eng, store, CHAMPION, HI - timedelta(days=8), HI)
    assert [r["metric"] for r in rows] == ["l0_lag_parity"] and "needs 7 days" in notes[0]
    rows, notes = checks.feature_drift(eng, store, {"model_version": "lgbm-y"}, HI, HI)
    assert "no reference_features.parquet for lgbm-y" in notes[0]


def _row(metric: str, value: float, br: bool, days: int, live: int = 10, feature: str = "") -> Any:
    ctx = {"consecutive_days": days, "live_days": live}
    return {"metric": metric, "feature": feature, "value": value, "br": br, "ctx": json.dumps(ctx)}


def test_psi_plus_mape_trigger_is_suppressed_while_a_shadow_runs(env: Any) -> None:
    eng = env[0]
    later = datetime.now(UTC) + timedelta(days=30)
    drift = [
        _row("rolling_mape_7d", 3.3, False, 0),  # above 1.2 x 2.6 = 3.12, below 1.5 x
        _row("ref_mape_30d", 2.6, False, 0),
        _row("psi", 0.4, True, 3, feature="temperature_2m_w"),
        _row("psi", 0.3, True, 3, feature="hdd_w"),
    ]
    assert daily.retrain_trigger(eng, drift, later) == "drift:psi+mape"
    assert daily.retrain_trigger(eng, drift, later, shadow_active=True) is None
    assert daily.retrain_trigger(eng, drift[:3], later) is None  # one hot feature only
    calm = [_row("rolling_mape_7d", 3.0, False, 0), *drift[1:]]
    assert daily.retrain_trigger(eng, calm, later) is None  # MAPE below 1.2 x the reference
