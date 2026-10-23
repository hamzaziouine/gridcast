"""Open-Meteo parsing, availability, pacing (no network, no database)."""

from __future__ import annotations

import json
from datetime import UTC, date, datetime
from pathlib import Path

import pandas as pd
import pytest

from gridcast.ingest import openmeteo as om

FIXTURES = Path(__file__).parents[1] / "fixtures"


def _load(name: str) -> object:
    return json.loads((FIXTURES / name).read_text())


def test_parse_historical_fixture() -> None:
    frame = om.parse_response(_load("openmeteo_historical_sample.json"))
    assert list(frame.columns) == ["valid_time", *om.WEATHER_COLUMNS]
    assert len(om.WEATHER_COLUMNS) == 54
    assert len(frame) == 48
    assert frame["valid_time"].iloc[0] == pd.Timestamp("2018-01-01 00:00", tz="UTC")
    assert frame["temperature_2m_toronto"].iloc[0] == pytest.approx(-16.4)
    # Accumulated variables start one hour later (DATA_NOTES §5.1): only 2018-01-01T00 is null.
    accumulated = ("shortwave_radiation_", "precipitation_", "snowfall_")
    first_row_nulls = frame.columns[frame.iloc[0].isna()]
    assert all(c.startswith(accumulated) for c in first_row_nulls)
    assert not frame.iloc[1:].isna().any().any()


def test_parse_live_fixture_span() -> None:
    frame = om.parse_response(_load("openmeteo_live_sample.json"))
    assert len(frame) == 72
    span = frame["valid_time"].iloc[-1] - frame["valid_time"].iloc[0]
    assert span == pd.Timedelta(hours=71)  # issue - 24 h .. issue + 47 h


def test_parse_rejects_wrong_location_order() -> None:
    payload = _load("openmeteo_historical_sample.json")
    assert isinstance(payload, list)
    payload[0], payload[1] = payload[1], payload[0]
    with pytest.raises(ValueError, match="order"):
        om.parse_response(payload)


def test_rows_have_synthetic_availability() -> None:
    frame = om.parse_response(_load("openmeteo_historical_sample.json"))
    rows = om.to_rows(frame, datetime(2026, 10, 23, 20, tzinfo=UTC))
    assert tuple(rows.columns) == om.ROW_COLUMNS
    assert (rows["valid_time"] - rows["available_at"] == pd.Timedelta(hours=24)).all()
    assert set(rows["available_at_kind"]) == {"modelled"}
    assert set(rows["source"]) == {"historical_forecast"}


def test_month_ranges() -> None:
    assert list(om.month_ranges(date(2018, 1, 1), date(2018, 3, 10))) == [
        (date(2018, 1, 1), date(2018, 1, 31)),
        (date(2018, 2, 1), date(2018, 2, 28)),
        (date(2018, 3, 1), date(2018, 3, 10)),
    ]


def test_request_weight() -> None:
    assert om.request_weight(6, 9, 31) == pytest.approx(6 * 31 / 14)
    assert om.request_weight(6, 9, 3) == 6


def test_rate_limiter_waits_when_minute_budget_is_spent() -> None:
    now = [0.0]
    slept: list[float] = []

    def sleep(s: float) -> None:
        slept.append(s)
        now[0] += s

    limiter = om.RateLimiter(budgets=((60, 30),), clock=lambda: now[0], sleep=sleep)
    for _ in range(2):
        limiter.acquire(13.3)
    assert slept == []
    limiter.acquire(13.3)  # 39.9 > 30 within the minute
    assert slept == [60.0]
