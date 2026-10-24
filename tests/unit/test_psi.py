"""PSI statistic and the season-matched reference slice (DATA_NOTES §19.3)."""

from __future__ import annotations

from datetime import UTC, date, datetime

import numpy as np
import pandas as pd

from gridcast.drift.psi import psi, season_slice


def test_psi_is_zero_for_the_same_distribution_and_large_for_a_shift() -> None:
    rng = np.random.default_rng(1)
    ref = rng.normal(0, 1, 20_000)
    assert psi(ref, rng.normal(0, 1, 2_000)) < 0.02
    assert psi(ref, rng.normal(1, 1, 2_000)) > 0.25
    assert np.isnan(psi(ref, np.array([np.nan])))


def test_psi_survives_constant_features() -> None:
    ref = np.zeros(1000)  # e.g. cdd_w all winter: every quantile edge is 0
    assert psi(ref, np.zeros(200)) < 1e-6
    assert psi(ref, np.full(200, 3.0)) > 0.25


def test_season_slice_wraps_the_year_end() -> None:
    t = pd.date_range(datetime(2023, 1, 1, tzinfo=UTC), periods=365, freq="D")
    ref = pd.DataFrame({"target_time": t})
    jan = season_slice(ref, [date(2026, 1, 3)])
    doys = set(pd.DatetimeIndex(jan["target_time"]).day_of_year)
    assert 18 in doys and 19 not in doys  # 3 + 15
    assert 353 in doys and 352 not in doys  # wraps: 365 + 3 - 15
