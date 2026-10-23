"""C1 / BUILD_PLAN §7.1: each modelled-availability rule returns the documented value."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

import pytest

from gridcast.config import REALTIME_LAG_H
from gridcast.ingest import availability as av
from gridcast.timeutil import day_target_times, he_to_utc


def z(*args: int) -> datetime:
    return datetime(*args, tzinfo=UTC)  # type: ignore[misc]


def test_realtime_lag_is_the_p05_provisional_constant() -> None:
    assert REALTIME_LAG_H == 1  # docs/DATA_NOTES.md §1.4, plan §3.0 decision 2


def test_data_cutoff_is_issue_plus_30_min() -> None:
    assert av.data_cutoff(z(2026, 11, 23, 15)) == z(2026, 11, 23, 15, 30)


def test_data_cutoff_requires_top_of_hour() -> None:
    with pytest.raises(ValueError, match="on the hour"):
        av.data_cutoff(z(2026, 11, 23, 15, 7))


@pytest.mark.parametrize("d", [date(2026, 1, 10), date(2026, 7, 15), date(2026, 11, 1)])
def test_pub_demand_every_hour_of_d_available_at_d_plus_1_13z(d: date) -> None:
    expected = datetime(d.year, d.month, d.day, 13, tzinfo=UTC) + timedelta(days=1)
    assert {av.pub_demand_available_at(t) for t in day_target_times(d)} == {expected}


def test_pub_demand_he24_uses_its_est_date_not_its_utc_date() -> None:
    # HE24 of 2025-12-31 starts 2026-01-01 04:00Z but belongs to EST date 2025-12-31.
    assert av.pub_demand_available_at(he_to_utc(date(2025, 12, 31), 24)) == z(2026, 1, 1, 13)


def test_realtime_totals_rule() -> None:
    t = z(2026, 10, 23, 19)
    assert av.realtime_totals_available_at(t) == t + timedelta(hours=REALTIME_LAG_H)
    assert av.realtime_totals_available_at(t, lag_h=2) == z(2026, 10, 23, 21)


def test_market_demand_uses_the_realtime_rule() -> None:
    # Plan §3.0 decision 1: PUB Market Demand history is stamped like RealtimeTotals.
    t = z(2026, 10, 23, 19)
    assert av.market_demand_available_at(t) == av.realtime_totals_available_at(t)


def test_historical_forecast_weather_is_valid_time_minus_24h() -> None:
    assert av.historical_forecast_available_at(z(2026, 1, 10, 5)) == z(2026, 1, 9, 5)


def test_live_snapshot_weather_is_fetched_at() -> None:
    fetched = z(2026, 11, 23, 15, 8, 41)
    assert av.live_snapshot_available_at(fetched) == fetched


@pytest.mark.parametrize("lag_h", [REALTIME_LAG_H, 1, 2, 3])
def test_l0_lag_equals_constant_for_every_issue_hour(lag_h: int) -> None:
    """Lag parity (§4.6): the newest qualifying realtime hour is exactly lag_h before issue."""
    for issue in [z(2026, 11, 1, 0) + timedelta(hours=h) for h in range(48)]:
        l0 = av.latest_realtime_target(issue, lag_h=lag_h)
        assert issue - l0 == timedelta(hours=lag_h)
        cutoff = av.data_cutoff(issue)
        assert av.realtime_totals_available_at(l0, lag_h=lag_h) <= cutoff
        assert av.realtime_totals_available_at(l0 + timedelta(hours=1), lag_h=lag_h) > cutoff
