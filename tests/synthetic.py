"""Deterministic synthetic demand history and weather for feature/baseline/harness tests."""

from __future__ import annotations

from datetime import UTC, datetime

import numpy as np
import pandas as pd

from gridcast.ingest.availability import historical_forecast_available_at, pub_demand_available_at
from gridcast.weather import WEATHER_COLUMNS


def hours(start: datetime, end: datetime) -> pd.DatetimeIndex:
    return pd.date_range(start, end, freq="h", tz=UTC, inclusive="left")


def ontario_series(idx: pd.DatetimeIndex, seed: int = 0) -> np.ndarray:
    rng = np.random.default_rng(seed)
    hour = idx.hour.to_numpy()
    dow = idx.day_of_week.to_numpy()
    base = 15_000 + 2_000 * np.sin(2 * np.pi * (hour - 11) / 24) - 800 * (dow >= 5)
    return np.round(base + rng.normal(0, 150, len(idx)), 0)


def raw_history(start: datetime, end: datetime, seed: int = 0) -> pd.DataFrame:
    """PUB rows with modelled stamps (D+1 13:00Z), Market = Ontario + 2,000 MW."""
    idx = hours(start, end)
    ont = ontario_series(idx, seed)
    return pd.DataFrame(
        {
            "target_time": idx,
            "source": "pub_demand",
            "market_demand_mw": ont + 2_000,
            "ontario_demand_mw": ont,
            "available_at": pd.DatetimeIndex(
                [pub_demand_available_at(t) for t in idx.to_pydatetime()]
            ),
            "available_at_kind": "modelled",
        }
    )


def weather_frame(start: datetime, end: datetime) -> pd.DataFrame:
    """Historical-forecast-shaped weather: stamps valid_time - 24 h, smooth daily cycle."""
    idx = hours(start, end)
    hour = idx.hour.to_numpy()
    data = {
        c: 10 + 8 * np.sin(2 * np.pi * (hour - 9) / 24) + i * 0.01
        for i, c in enumerate(WEATHER_COLUMNS)
    }
    df = pd.DataFrame(data)
    df.insert(0, "valid_time", idx)
    df.insert(
        1,
        "available_at",
        pd.DatetimeIndex([historical_forecast_available_at(t) for t in idx.to_pydatetime()]),
    )
    return df
