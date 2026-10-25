"""C18: the backtest stage of the gate, the shadow slot rule and the `fast` training grid."""

from __future__ import annotations

from datetime import UTC, datetime

import numpy as np
import pandas as pd
import pytest

from gridcast.features.build import to_ns
from gridcast.jobs.retrain import backtest_stage, champion_profile, slot_action, weekly_slot
from gridcast.models.lgbm import PROFILES, LgbmForecaster, keep_issue


@pytest.mark.parametrize(
    ("kind", "mape", "ref", "cov", "passed"),
    [
        ("refit", 2.612, 2.612, 0.82, True),  # identical procedure: exact reproduction
        ("refit", 2.625, 2.612, 0.82, True),  # +0.50 % relative: inside the tolerance
        ("refit", 2.627, 2.612, 0.82, False),  # +0.57 %: not the same procedure
        ("refit", 2.590, 2.612, 0.82, False),  # -0.84 %: a refit must not change the numbers
        ("refit", 2.612, 2.612, 0.86, False),  # coverage outside COVERAGE_BAND
        ("challenger", 2.559, 2.612, 0.80, True),  # >= 2 % better
        ("challenger", 2.561, 2.612, 0.80, False),  # 1.95 % better: not enough
        ("challenger", 2.400, 2.612, 0.75, False),  # better but coverage below the band
    ],
)
def test_backtest_stage(kind: str, mape: float, ref: float, cov: float, passed: bool) -> None:
    res = backtest_stage(kind, mape, ref, cov)
    assert res.passed is passed, res.why


def test_backtest_stage_rejects_unknown_kind() -> None:
    with pytest.raises(ValueError, match="unknown kind"):
        backtest_stage("tcn", 2.0, 2.6, 0.8)


def test_slot_action() -> None:
    assert slot_action(None, "refit") == "start"
    assert slot_action(None, "challenger") == "start"
    assert slot_action({"kind": "refit"}, "challenger") == "preempt"
    assert slot_action({"kind": "refit"}, "refit") == "queue"  # never replaced before the gate
    assert slot_action({"kind": "challenger"}, "challenger") == "queue"
    assert slot_action({"kind": "challenger"}, "refit") == "queue"  # a refit never pre-empts


@pytest.mark.parametrize(
    ("now", "slot"),
    [
        ("2026-10-25T03:00:00", "2026-10-25T03:00:00"),  # the Sunday slot itself
        ("2026-10-25T03:00:41", "2026-10-25T03:00:00"),  # Cloudflare, seconds late
        ("2026-10-25T06:37:00", "2026-10-25T03:00:00"),  # GitHub cron, hours late
        ("2026-10-26T01:15:00", "2026-10-25T03:00:00"),  # delayed past midnight into Monday
        ("2026-10-25T02:59:59", "2026-10-18T03:00:00"),  # before the slot: last week's
        ("2026-10-31T23:00:00", "2026-10-25T03:00:00"),  # Saturday
        ("2026-11-01T03:00:00", "2026-11-01T03:00:00"),  # next Sunday
    ],
)
def test_weekly_slot(now: str, slot: str) -> None:
    t = datetime.fromisoformat(now).replace(tzinfo=UTC)
    assert weekly_slot(t) == datetime.fromisoformat(slot).replace(tzinfo=UTC)


def test_champion_profile_defaults_to_full_for_pre_adr13_pointers() -> None:
    assert champion_profile({"model_version": "lgbm-2026.10.23-49ca"}) == "full"
    assert champion_profile({"profile": "fast"}) == "fast"


def test_fast_grid_keeps_half_and_every_hour_of_day_for_every_horizon() -> None:
    issues = pd.date_range(datetime(2025, 1, 1, tzinfo=UTC), periods=24 * 14, freq="h")
    keep = keep_issue(to_ns(issues), "fast")
    assert keep.sum() == len(issues) // 2
    kept = issues[keep]
    for h in (1, 2, 13, 24):
        assert set((kept + pd.Timedelta(hours=h)).hour) == set(range(24)), h
    assert keep_issue(to_ns(issues), "full").all()


def test_profile_survives_the_artifact_round_trip() -> None:
    model = LgbmForecaster(profile="fast")
    again = LgbmForecaster.from_bytes(model.to_bytes())
    assert again.profile == "fast"
    assert again.params["max_bin"] == 63 and not PROFILES["fast"].early_stopping
    assert np.isclose(LgbmForecaster().params["learning_rate"], 0.03)
