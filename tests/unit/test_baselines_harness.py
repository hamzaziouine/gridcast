"""C7: baselines (exact values, as-of), interval offsets, rolling-origin harness (synthetic)."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

import numpy as np
import pandas as pd
import pytest

from gridcast.backtest import harness as hz
from gridcast.features.build import DemandHistory, LeakageError, VersionedSeries, to_ns
from gridcast.models.baselines import baseline_frame, baseline_p50
from gridcast.models.intervals import LEAD_TIME_ALLOWANCE, IntervalOffsets, fit_offsets
from tests.synthetic import raw_history, weather_frame

START = datetime(2024, 1, 1, tzinfo=UTC)
END = datetime(2024, 5, 1, tzinfo=UTC)


@pytest.fixture(scope="module")
def raw() -> pd.DataFrame:
    return raw_history(START, END)


def test_baselines_exact_values(raw: pd.DataFrame) -> None:
    hist = DemandHistory.from_raw(raw)
    by_t = raw.set_index("target_time")["ontario_demand_mw"]
    issue = datetime(2024, 3, 10, 2, tzinfo=UTC)  # 2024-03-09 21:00 EST
    ns = to_ns([issue])
    for h in (1, 12, 24):
        tgt = issue + timedelta(hours=h)
        v, _ = baseline_p50("seasonal_naive_168", hist, ns, h)
        assert v[0] == by_t[tgt - timedelta(hours=168)]
        # PUB rule at 02:30Z: EST dates up to 03-08 are published (03-09 13:00Z); 03-09 is not.
        v, _ = baseline_p50("seasonal_naive_24", hist, ns, h)
        lag = next(
            k
            for k in (24, 48, 72)
            if tgt - timedelta(hours=k) < datetime(2024, 3, 9, 5, tzinfo=UTC)
        )
        assert v[0] == by_t[tgt - timedelta(hours=lag)]
    v, a = baseline_p50("persistence", hist, ns, 5)
    assert v[0] == by_t[datetime(2024, 3, 9, 4, tzinfo=UTC)]  # 03-08 HE24
    assert a[0] <= ns[0] + 30 * 60 * 10**9


def test_baselines_ignore_rows_after_cutoff(raw: pd.DataFrame) -> None:
    issue = datetime(2024, 3, 10, 2, tzinfo=UTC)
    late = raw[raw["target_time"] == issue - timedelta(hours=167)].assign(
        ontario_demand_mw=1.0,
        available_at=pd.Timestamp(issue + timedelta(minutes=31)),
        available_at_kind="observed",
    )
    a = baseline_frame(DemandHistory.from_raw(raw), to_ns([issue]))
    b = baseline_frame(DemandHistory.from_raw(pd.concat([raw, late])), to_ns([issue]))
    pd.testing.assert_frame_equal(a, b)


def test_interval_offsets_cover_nominal_on_gaussian_noise() -> None:
    rng = np.random.default_rng(1)
    n = 24 * 2000
    h = np.tile(np.arange(1, 25), n // 24)
    p50 = np.full(n, 15000.0)
    sd = 0.01 + 0.001 * h  # widens with horizon
    actual = p50 * (1 + rng.normal(0, sd))
    off = fit_offsets(pd.DataFrame({"horizon": h, "p50": p50, "actual": actual}))
    p10, p90 = off.apply(p50, h)
    cov = np.mean((actual >= p10) & (actual <= p90))
    assert 0.78 <= cov <= 0.82
    widths = [off.q90[k] - off.q10[k] for k in range(1, 25)]
    assert widths[-1] > widths[0]
    wide = IntervalOffsets(off.q10, off.q90, LEAD_TIME_ALLOWANCE)
    w10, w90 = wide.apply(p50, h)
    assert np.allclose(w90 - p50, 1.15 * (p90 - p50))
    assert IntervalOffsets.from_json(wide.to_json()) == wide


def test_default_folds_are_twelve_months_ending_before_the_data() -> None:
    folds = hz.default_folds(datetime(2026, 10, 23, 4, tzinfo=UTC))
    assert [f.k for f in folds] == list(range(1, 13))
    assert folds[0].month == date(2025, 10, 1) and folds[-1].month == date(2026, 9, 1)
    assert folds[-1].start == datetime(2026, 9, 1, 5, tzinfo=UTC)
    assert folds[-1].train_end == folds[-1].start - timedelta(days=2)


def test_peak_thresholds_need_720_hours() -> None:
    idx = pd.date_range("2024-01-01 05:00", periods=24 * 40, freq="h", tz=UTC)
    act = pd.Series(np.arange(len(idx), dtype=float), index=idx)
    thr = hz.peak_thresholds(act)
    assert np.isnan(thr[date(2024, 1, 30)])
    expected = np.percentile(np.arange(0, 720, dtype=float), 90)  # EST days 01-01..01-30
    assert thr[date(2024, 1, 31)] == pytest.approx(expected)


def test_harness_runs_baselines_with_leakage_assertion(raw: pd.DataFrame) -> None:
    wx = weather_frame(START, END)
    act = raw.set_index("target_time")["ontario_demand_mw"]
    folds = [hz.Fold(1, date(2024, 3, 1)), hz.Fold(2, date(2024, 4, 1))]
    res = hz.run_backtest(raw, wx, act, folds=folds, with_lgbm=False, log=lambda _: None)
    s = res.summary()
    assert set(s.index) == {"seasonal_naive_168", "seasonal_naive_24", "persistence"}
    assert 0 < s.loc["seasonal_naive_168", "mape"] < 10
    # every test issue has its L0 exactly REALTIME_LAG_H = 1 h back ("<lag>:<count>", one bucket)
    assert all(v.startswith("1:") and "," not in v for v in res.lag_parity["l0_lag_h"])
    assert (res.lag_parity[["null_l0", "null_mean24", "null_lag168"]] == 0).all().all()


def test_harness_trips_on_a_leaking_selection(
    raw: pd.DataFrame, monkeypatch: pytest.MonkeyPatch
) -> None:
    wx = weather_frame(START, END)
    act = raw.set_index("target_time")["ontario_demand_mw"]
    monkeypatch.setattr(
        VersionedSeries,
        "_asof_mask",
        staticmethod(lambda avail, cutoffs: avail != np.iinfo(np.int64).max),
    )
    with pytest.raises(LeakageError):
        hz.run_backtest(raw, wx, act, folds=[hz.Fold(1, date(2024, 4, 1))], with_lgbm=False)
