"""C19: the gate's pure rules (BUILD_PLAN §4.7 items 2-4, 7, 8), table-driven."""

from __future__ import annotations

import math
from datetime import UTC, datetime, timedelta

import pandas as pd
import pytest

from gridcast.jobs.gate import PairStats, pair_stats, rollback_decision, shadow_decision
from gridcast.jobs.predict import _prev_active

D = timedelta(days=1)
OK = dict(n_pairs=8000, n_targets=340, mape_c=2.60, mape_s=2.50, n_peak=40, peak_c=3.0,
          peak_s=3.1, cov_c=0.82, cov_s=0.80)  # fmt: skip


def st(**kw: float) -> PairStats:
    return PairStats(**{**OK, **kw})  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("kind", "age", "stats", "early", "action", "why"),
    [
        ("challenger", 15 * D, st(), False, "promote", "MAPE 2.500"),
        ("challenger", 13 * D, st(), False, "hold", "too young"),  # < 14 days
        ("challenger", 15 * D, st(n_targets=299), False, "hold", "too young"),  # < 300 targets
        ("refit", 15 * D, st(mape_s=2.60), False, "promote", ""),  # refit: 1.00 x passes on a tie
        ("challenger", 15 * D, st(mape_s=2.58), False, "reject", "0.99 x"),  # challenger needs 1 %
        ("refit", 15 * D, st(mape_s=2.61), False, "reject", "1.00 x"),
        ("challenger", 15 * D, st(n_peak=29), False, "hold", "insufficient peak sample"),
        ("challenger", 15 * D, st(n_peak=29, mape_s=2.9), False, "reject", "MAPE"),  # MAPE first
        ("challenger", 15 * D, st(peak_s=3.21), False, "reject", "peak"),  # > champion + 0.2 pt
        ("challenger", 15 * D, st(peak_s=3.2), False, "promote", ""),  # exactly + 0.2: allowed
        ("challenger", 15 * D, st(cov_s=0.86), False, "reject", "coverage"),  # 0.06 > 0.02 + 0.02
        ("challenger", 15 * D, st(cov_s=0.84), False, "promote", ""),  # 0.04 <= 0.04
        ("challenger", 15 * D, st(cov_s=math.nan), False, "reject", "coverage"),
        ("refit", 8 * D, st(n_targets=160), True, "promote", ""),  # drift_early, refit
        ("refit", 8 * D, st(n_targets=160), False, "hold", "too young"),
        ("challenger", 8 * D, st(n_targets=160), True, "hold", "too young"),  # early: refit only
        ("refit", 6 * D, st(n_targets=160), True, "hold", "too young"),  # early needs 7 days
    ],
)
def test_shadow_decision(
    kind: str, age: timedelta, stats: PairStats, early: bool, action: str, why: str
) -> None:
    d = shadow_decision(kind, age, stats, early)
    assert d.action == action, d.reason
    assert why in d.reason


def test_early_promotion_is_labelled_drift_early() -> None:
    assert shadow_decision("refit", 8 * D, st(n_targets=160), True).triggered_by == "drift_early"
    assert shadow_decision("refit", 15 * D, st(), True).triggered_by == "gate"


def test_cooldown_turns_a_pass_into_a_wait_and_blocked_versions_are_rejected() -> None:
    until = datetime(2027, 1, 20, tzinfo=UTC)
    d = shadow_decision("challenger", 15 * D, st(), cooldown_until=until)
    assert d.action == "hold" and "cooldown until 2027-01-20" in d.reason
    assert shadow_decision("challenger", 15 * D, st(), blocked=True).action == "reject"


@pytest.mark.parametrize(
    ("n", "new", "prev", "holiday", "action"),
    [
        (60, 3.5, 3.0, False, "rollback"),  # 3.5 > 1.15 x 3.0 = 3.45
        (60, 3.4, 3.0, False, "hold"),  # within 15 %
        (47, 9.0, 3.0, False, "hold"),  # < 48 paired target hours
        (60, 3.5, 3.0, True, "hold"),  # explained by a holiday
    ],
)
def test_rollback_decision(n: int, new: float, prev: float, holiday: bool, action: str) -> None:
    assert rollback_decision(n, new, prev, holiday).action == action


def test_pair_stats_use_identical_pairs_only() -> None:
    t0 = pd.Timestamp("2027-01-05T00:00Z")
    rows = []
    for k in range(4):
        tt = t0 + pd.Timedelta(hours=k + 1)
        rows.append(("champion", t0 + pd.Timedelta(hours=k), tt, 102.0, k == 0))
        if k < 3:  # the shadow misses the last pair: it must not count for either side
            rows.append(("shadow", t0 + pd.Timedelta(hours=k), tt, 101.0, k == 0))
    df = pd.DataFrame(rows, columns=["model_name", "issue_time", "target_time", "p50", "is_peak"])
    df["actual"], df["p10"], df["p90"] = 100.0, 95.0, 105.0
    s = pair_stats(df, "champion", "shadow")
    assert (s.n_pairs, s.n_targets, s.n_peak) == (3, 3, 1)
    assert s.mape_c == pytest.approx(2.0) and s.mape_s == pytest.approx(1.0)
    assert s.cov_c == 1.0 and s.peak_c == pytest.approx(2.0)


def test_prev_champion_rows_only_for_7_days_after_a_gated_promotion() -> None:
    champ = {"promoted_at": "2027-01-16T06:31:00+00:00", "previous_version": "lgbm-old"}
    t = datetime(2027, 1, 16, 6, tzinfo=UTC)
    assert _prev_active(champ, t)  # the promotion hour itself
    assert _prev_active(champ, t + timedelta(days=6, hours=23))
    assert not _prev_active(champ, t + timedelta(days=7, hours=1))
    assert not _prev_active(champ, t - timedelta(hours=1))
    assert not _prev_active({"promoted_at": "2026-10-24T01:18:02Z"}, t)  # bootstrap: no previous
