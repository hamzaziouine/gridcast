"""C6: as-of feature builder, leakage tripwires, lag parity, calendar (BUILD_PLAN §7.1)."""

from __future__ import annotations

import json
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from gridcast.config import REALTIME_LAG_H
from gridcast.features import calendar as cal
from gridcast.features.build import (
    DemandHistory,
    FeatureBuilder,
    LeakageError,
    MissingInputError,
    VersionedSeries,
    to_ns,
)
from gridcast.features.schema import FEATURE_COLUMNS, FEATURE_SCHEMA_VERSION
from gridcast.ingest.availability import data_cutoff
from gridcast.ingest.openmeteo import parse_response
from tests.synthetic import raw_history, weather_frame

FIXTURES = Path(__file__).parents[1] / "fixtures"
START = datetime(2024, 1, 1, tzinfo=UTC)
END = datetime(2024, 3, 1, tzinfo=UTC)
ISSUE = datetime(2024, 2, 20, 15, tzinfo=UTC)


@pytest.fixture(scope="module")
def raw() -> pd.DataFrame:
    return raw_history(START, END)


@pytest.fixture(scope="module")
def weather() -> pd.DataFrame:
    return weather_frame(START, END)


def build(raw: pd.DataFrame, weather: pd.DataFrame, issues: list[datetime]) -> pd.DataFrame:
    return FeatureBuilder(DemandHistory.from_raw(raw), weather).frame(issues)


def test_schema_version_and_shape(raw: pd.DataFrame, weather: pd.DataFrame) -> None:
    assert FEATURE_SCHEMA_VERSION == 1
    f = build(raw, weather, [ISSUE])
    assert len(f) == 24
    assert list(f["horizon"]) == [float(h) for h in range(1, 25)]
    assert list(f.columns[-len(FEATURE_COLUMNS) :]) == list(FEATURE_COLUMNS)
    assert (f["target_time"] == f["issue_time"] + pd.to_timedelta(f["horizon"], unit="h")).all()
    must = ["mkt_l0", "mkt_mean24", "mkt_mean168", "ont_last", "ont_lag168", "temp_w_mean24"]
    assert f[must].notna().all().all()


def test_l0_lag_equals_modelled_constant_for_every_issue_hour(
    raw: pd.DataFrame, weather: pd.DataFrame
) -> None:
    issues = [ISSUE.replace(hour=0) + timedelta(hours=k) for k in range(24)]
    f = build(raw, weather, issues)
    lag = (f["issue_time"] - f["l0_target_time"]) / pd.Timedelta(hours=1)
    assert set(lag) == {float(REALTIME_LAG_H)}
    assert set(f["mkt_l0_age_h"]) == {float(REALTIME_LAG_H)}


def test_values_are_as_of(raw: pd.DataFrame, weather: pd.DataFrame) -> None:
    f = build(raw, weather, [ISSUE]).iloc[0]
    by_t = raw.set_index("target_time")
    l0_t = ISSUE - timedelta(hours=REALTIME_LAG_H)
    assert f["mkt_l0"] == by_t.loc[l0_t, "market_demand_mw"]
    # Ontario under the PUB rule: issue 15Z (cutoff 15:30Z) on 2024-02-20 sees EST date 02-19
    # (published 02-20 13:00Z), whose last hour HE24 starts 02-20 04:00Z -> age 11 h.
    assert f["ont_last_age_h"] == 11
    assert f["ont_last"] == by_t.loc[ISSUE - timedelta(hours=11), "ontario_demand_mw"]
    assert f["ont_lag168"] == by_t.loc[ISSUE + timedelta(hours=1 - 168), "ontario_demand_mw"]


def test_late_rows_are_invisible_and_features_byte_identical(
    raw: pd.DataFrame, weather: pd.DataFrame
) -> None:
    before = build(raw, weather, [ISSUE])
    cut = pd.Timestamp(data_cutoff(ISSUE))
    # 1) a simulated daily PUB ingest after the cutoff: tomorrow's rows, observed stamps
    new_rows = raw_history(ISSUE, ISSUE + timedelta(hours=10), seed=9)
    new_rows["available_at"] = cut + pd.Timedelta(minutes=1)
    new_rows["available_at_kind"] = "observed"
    # 2) a PUB _vNNN revision, observed after the cutoff, of two hours already published before it:
    #    the newest visible Ontario hour (also inside the Market windows) and the target-168 h hour
    rev = raw[raw["target_time"].isin([ISSUE - timedelta(hours=11), ISSUE - timedelta(hours=167)])]
    rev = rev.assign(
        ontario_demand_mw=rev["ontario_demand_mw"] + 999,
        market_demand_mw=rev["market_demand_mw"] + 999,
        available_at=cut + pd.Timedelta(minutes=1),
        available_at_kind="observed",
    )
    after = build(pd.concat([raw, new_rows, rev], ignore_index=True), weather, [ISSUE])
    pd.testing.assert_frame_equal(before, after)
    assert before.to_csv().encode() == after.to_csv().encode()


def test_revision_visible_once_published(raw: pd.DataFrame, weather: pd.DataFrame) -> None:
    rev = raw[raw["target_time"] == ISSUE - timedelta(hours=167)].assign(
        ontario_demand_mw=lambda d: d["ontario_demand_mw"] + 999,
        available_at=pd.Timestamp(data_cutoff(ISSUE)) - pd.Timedelta(minutes=1),
        available_at_kind="observed",
    )
    f = build(pd.concat([raw, rev], ignore_index=True), weather, [ISSUE]).iloc[0]
    assert f["ont_lag168"] == rev["ontario_demand_mw"].iloc[0]


def test_tripwire_weather_row_after_cutoff_raises(raw: pd.DataFrame, weather: pd.DataFrame) -> None:
    w = weather.copy()
    target = ISSUE + timedelta(hours=24)
    w.loc[w["valid_time"] == target, "available_at"] = pd.Timestamp(
        data_cutoff(ISSUE)
    ) + pd.Timedelta(minutes=1)
    with pytest.raises(LeakageError):
        build(raw, w, [ISSUE])


def test_tripwire_demand_row_after_cutoff_raises_if_selection_leaks(
    raw: pd.DataFrame, weather: pd.DataFrame, monkeypatch: pytest.MonkeyPatch
) -> None:
    late = raw.copy()
    l0_t = ISSUE - timedelta(hours=REALTIME_LAG_H)
    # make the L0 hour a realtime row stamped one minute after the cutoff (its PUB twin dropped)
    late = late[late["target_time"] != l0_t]
    row = raw[raw["target_time"] == l0_t].assign(
        source="realtime_totals",
        ontario_demand_mw=np.nan,
        available_at=pd.Timestamp(data_cutoff(ISSUE)) + pd.Timedelta(minutes=1),
        available_at_kind="observed",
    )
    late = pd.concat([late, row], ignore_index=True)
    # correct selection: the late row is invisible, L0 falls back one hour (live rule)
    f = build(late, weather, [ISSUE]).iloc[0]
    assert f["mkt_l0_age_h"] == REALTIME_LAG_H + 1
    # a selection bug that ignores the cutoff is caught by the independent assertion
    monkeypatch.setattr(
        VersionedSeries,
        "_asof_mask",
        staticmethod(lambda avail, cutoffs: avail != np.iinfo(np.int64).max),
    )
    with pytest.raises(LeakageError):
        build(late, weather, [ISSUE])


def test_missing_weather_raises(raw: pd.DataFrame, weather: pd.DataFrame) -> None:
    w = weather[weather["valid_time"] < ISSUE + timedelta(hours=12)]
    with pytest.raises(MissingInputError):
        build(raw, w, [ISSUE])


def test_live_snapshot_and_store_frames_have_identical_columns() -> None:
    payload = json.loads((FIXTURES / "openmeteo_live_sample.json").read_text())
    snap = parse_response(payload)
    fetched = datetime(2026, 10, 23, 20, 27, tzinfo=UTC)
    snap["available_at"] = pd.Timestamp(fetched)
    issue = datetime(2026, 10, 23, 20, tzinfo=UTC)
    raw = raw_history(issue - timedelta(days=20), issue)
    live = FeatureBuilder(DemandHistory.from_raw(raw), snap).frame([issue])
    store_w = weather_frame(issue - timedelta(days=2), issue + timedelta(days=2))
    store = FeatureBuilder(DemandHistory.from_raw(raw), store_w).frame([issue])
    assert list(live.columns) == list(store.columns)
    rolling = ["temp_w_mean24", "temp_w_delta3", "temperature_2m_w"]
    assert live[rolling].notna().all().all()
    assert store[rolling].notna().all().all()


def test_versioned_series_lookup_picks_newest_visible() -> None:
    t = to_ns(pd.DatetimeIndex([datetime(2024, 1, 1, h, tzinfo=UTC) for h in (0, 0, 0, 1)]))
    a = t + np.array([1, 5, 9, 1]) * 3_600_000_000_000
    s = VersionedSeries(t, np.array([1.0, 2.0, 3.0, 4.0]), a)
    cut = t[0] + np.array([0, 1, 6, 10]) * 3_600_000_000_000
    v, _ = s.lookup(np.full(4, t[0]), cut)
    assert np.isnan(v[0]) and list(v[1:]) == [1.0, 2.0, 3.0]


# ---------------------------------------------------------------- calendar


def ts(*a: int) -> datetime:
    return datetime(*a, tzinfo=UTC)  # type: ignore[misc]


@pytest.mark.parametrize(
    ("d", "hid"),
    [(date(2027, 2, 15), 2), (date(2026, 8, 3), 6), (date(2026, 12, 25), 9), (date(2026, 8, 4), 0)],
)
def test_holidays(d: date, hid: int) -> None:
    assert cal.holiday_id(d) == hid


def test_calendar_civil_date_at_edt_boundary() -> None:
    # Civic Holiday 2026-08-03 (EDT): civil 00:00-01:00 = 04:00Z = HE24 of EST date 2026-08-02.
    f = cal.calendar_features(pd.DatetimeIndex([ts(2026, 8, 3, 4), ts(2026, 8, 3, 3)]))
    assert list(f["is_holiday"]) == [1.0, 0.0]
    assert list(f["hour_est"]) == [23.0, 22.0]
    assert list(f["civil_hour"]) == [0.0, 23.0]


def test_long_weekend_bridge_and_xmas() -> None:
    assert cal.is_long_weekend(date(2026, 8, 1))  # Sat before Civic Holiday Monday
    assert cal.is_long_weekend(date(2026, 8, 3))
    assert not cal.is_long_weekend(date(2026, 8, 8))
    assert cal.is_bridge_day(date(2027, 7, 2))  # Fri after Canada Day (Thu)
    assert not cal.is_bridge_day(date(2027, 7, 5))
    f = cal.calendar_features(pd.DatetimeIndex([ts(2026, 12, 24, 17), ts(2027, 1, 3, 17)]))
    assert list(f["xmas_period"]) == [1.0, 0.0]
    assert cal.days_to_next_holiday(date(2026, 12, 20)) == 5
