"""Data contracts (pandera) for the raw tables (C5, BUILD_PLAN §7.2).

Each ingest path validates its frame here before writing; a failure raises
`pandera.errors.SchemaError(s)` and nothing is written (the caller's transaction never starts).
Ranges are wide physical bounds, meant to catch a parsing or unit mistake, not to judge data.
"""

from __future__ import annotations

from datetime import datetime, timedelta

import pandas as pd
import pandera.pandas as pa

from gridcast.timeutil import he_to_utc
from gridcast.weather import CITIES, VARIABLES

# Ontario Demand 2015-2026 spans ~9.6-25.5 GW; Market Demand runs ~0.5-4 GW above it.
ONTARIO_MW = (5_000.0, 30_000.0)
MARKET_MW = (5_000.0, 35_000.0)
CLOCK_SKEW = timedelta(minutes=5)
NEWEST_SNAPSHOT_MAX_AGE = timedelta(days=3)


def _utc(s: pd.Series) -> bool:
    return str(getattr(s.dt, "tz", None)) == "UTC"


UTC_TS = pa.Check(_utc, element_wise=False, error="timestamps must be tz-aware UTC")
ON_THE_HOUR = pa.Check(
    lambda s: (s.dt.minute == 0) & (s.dt.second == 0) & (s.dt.microsecond == 0),
    error="must be on the hour",
)


def _target_matches_he(df: pd.DataFrame) -> bool:
    expected = [
        he_to_utc(d, int(h)) for d, h in zip(df["local_date"], df["hour_ending"], strict=True)
    ]
    return bool((pd.DatetimeIndex(df["target_time"]) == pd.DatetimeIndex(expected)).all())


def _source_columns(df: pd.DataFrame) -> pd.Series:
    pub = df["source"] == "pub_demand"
    ok_pub = pub & df["ontario_demand_mw"].notna() & df["market_demand_mw"].notna()
    ok_rt = (
        ~pub
        & df["ontario_demand_mw"].isna()
        & df["market_demand_mw"].notna()
        & (df["n_intervals"] == 12)
    )
    return ok_pub | ok_rt


RawDemand = pa.DataFrameSchema(
    {
        "target_time": pa.Column(checks=[UTC_TS, ON_THE_HOUR], nullable=False),
        "source": pa.Column(str, pa.Check.isin(["pub_demand", "realtime_totals"])),
        "file_name": pa.Column(str, pa.Check.str_length(min_value=1)),
        "file_version": pa.Column(nullable=True),
        "local_date": pa.Column(nullable=False),
        "hour_ending": pa.Column(checks=pa.Check.in_range(1, 24)),
        "market_demand_mw": pa.Column(
            float, pa.Check.in_range(*MARKET_MW), nullable=True, coerce=True
        ),
        "ontario_demand_mw": pa.Column(
            float, pa.Check.in_range(*ONTARIO_MW), nullable=True, coerce=True
        ),
        "n_intervals": pa.Column(nullable=True),
        "available_at": pa.Column(checks=UTC_TS, nullable=False),
        "available_at_kind": pa.Column(str, pa.Check.isin(["observed", "modelled"])),
        "fetched_at": pa.Column(checks=UTC_TS, nullable=False),
        "http_last_modified": pa.Column(nullable=True),
    },
    checks=[
        pa.Check(_target_matches_he, error="target_time != he_to_utc(local_date, hour_ending)"),
        pa.Check(_source_columns, error="source/column rule (plan §3.0 decision 1) violated"),
        pa.Check(
            lambda df: df["available_at"] <= df["fetched_at"] + CLOCK_SKEW,
            error="available_at after fetched_at (a stamp from the future)",
        ),
    ],
    unique=["target_time", "source", "file_name"],
    strict=True,
    ordered=True,
    name="RawDemand",
)

_WEATHER_RANGES = {
    "temperature_2m": (-60.0, 50.0),
    "apparent_temperature": (-75.0, 65.0),
    "relative_humidity_2m": (0.0, 100.0),
    "dew_point_2m": (-75.0, 40.0),
    "wind_speed_10m": (0.0, 250.0),
    "cloud_cover": (0.0, 100.0),
    "shortwave_radiation": (0.0, 1_400.0),
    "precipitation": (0.0, 250.0),
    "snowfall": (0.0, 150.0),
}
_WEATHER_VALUE_COLUMNS = {
    f"{var}_{city.key}": pa.Column(
        float, pa.Check.in_range(*_WEATHER_RANGES[var]), nullable=True, coerce=True
    )
    for city in CITIES
    for var in VARIABLES
}

# The wide frame both the training store and the live snapshot use (migration 0002).
WeatherFrame = pa.DataFrameSchema(
    {
        "valid_time": pa.Column(checks=[UTC_TS, ON_THE_HOUR], unique=True, nullable=False),
        **_WEATHER_VALUE_COLUMNS,
    },
    checks=[
        # Nulls are allowed (the archive's first hour has no accumulated variables) but a frame
        # that is mostly empty means the API returned nothing useful.
        pa.Check(
            lambda df: df[list(_WEATHER_VALUE_COLUMNS)].notna().to_numpy().mean() >= 0.95,
            error="more than 5 % of weather values are null",
        ),
    ],
    strict=True,
    ordered=True,
    name="WeatherFrame",
)

WeatherHourly = pa.DataFrameSchema(
    {
        "valid_time": pa.Column(checks=[UTC_TS, ON_THE_HOUR], unique=True, nullable=False),
        "source": pa.Column(str, pa.Check.isin(["historical_forecast", "live_forecast_backfill"])),
        "available_at": pa.Column(checks=UTC_TS, nullable=False),
        "available_at_kind": pa.Column(str, pa.Check.isin(["observed", "modelled"])),
        "fetched_at": pa.Column(checks=UTC_TS, nullable=False),
        **_WEATHER_VALUE_COLUMNS,
    },
    checks=[
        pa.Check(
            lambda df: (
                (df["source"] != "historical_forecast")
                | (df["valid_time"] - df["available_at"] == pd.Timedelta(hours=24))
            ),
            error="historical_forecast rows must carry available_at = valid_time - 24 h",
        ),
    ],
    strict=True,
    ordered=True,
    name="WeatherHourly",
)

IesoSnapshot = pa.DataFrameSchema(
    {
        "target_time": pa.Column(checks=[UTC_TS, ON_THE_HOUR], unique=True, nullable=False),
        "energy_mw": pa.Column(float, pa.Check.in_range(*ONTARIO_MW), coerce=True),
    },
    checks=pa.Check(lambda df: len(df) == 24, error="a delivery date has 24 hours (EST, no DST)"),
    strict=True,
    name="IesoSnapshot",
)


def validate_weather_snapshot(frame: pd.DataFrame, fetched_at: datetime) -> pd.DataFrame:
    """Live snapshot: WeatherFrame plus the span [issue - 24 h, issue + 47 h] (DATA_NOTES §9.1).

    `issue` is the top of the hour of the fetch; 72 hourly rows with no gap.
    """
    WeatherFrame.validate(frame)
    issue = pd.Timestamp(fetched_at).tz_convert("UTC").floor("h")
    expected = pd.date_range(
        issue - pd.Timedelta(hours=24), issue + pd.Timedelta(hours=47), freq="h"
    )
    got = pd.DatetimeIndex(frame["valid_time"])
    if not expected.isin(got).all():
        missing = expected.difference(got)
        raise ValueError(f"live weather snapshot does not cover the span: missing {list(missing)}")
    return frame


def validate_ieso_snapshot(created_at: datetime, rows: pd.DataFrame, fetched_at: datetime) -> None:
    """Every version: 24 hourly rows in range, created no later than we fetched it."""
    IesoSnapshot.validate(rows)
    if created_at > fetched_at + CLOCK_SKEW:
        raise ValueError(f"Adequacy3 CreatedAt {created_at} is after fetched_at {fetched_at}")


def check_newest_snapshot(newest_created_at: datetime, fetched_at: datetime) -> None:
    """The newest version seen in a run must be recent (catches a CreatedAt timezone mistake)."""
    if not fetched_at - NEWEST_SNAPSHOT_MAX_AGE <= newest_created_at <= fetched_at + CLOCK_SKEW:
        raise ValueError(
            f"newest Adequacy3 CreatedAt {newest_created_at} not within 3 days before {fetched_at}"
        )


def _interval_order(df: pd.DataFrame) -> pd.Series:
    lo_ok = df["p10"].isna() | (df["p10"] <= df["p50"])
    hi_ok = df["p90"].isna() | (df["p90"] >= df["p50"])
    return lo_ok & hi_ok


# Rows `gridcast predict` writes (plan §7.2): one issue time, 24 horizons per model name.
Predictions = pa.DataFrameSchema(
    {
        "issue_time": pa.Column(checks=[UTC_TS, ON_THE_HOUR], nullable=False),
        "target_time": pa.Column(checks=[UTC_TS, ON_THE_HOUR], nullable=False),
        "horizon": pa.Column(int, pa.Check.in_range(1, 24), coerce=True),
        "model_name": pa.Column(
            str,
            pa.Check.isin(
                [
                    "champion",
                    "shadow",
                    "prev_champion",
                    "seasonal_naive_168",
                    "seasonal_naive_24",
                    "persistence",
                    "ieso_adequacy",
                ]
            ),
        ),
        "model_version": pa.Column(str, pa.Check.str_length(min_value=1)),
        "p10": pa.Column(float, nullable=True, coerce=True),
        "p50": pa.Column(float, pa.Check.in_range(*ONTARIO_MW), coerce=True),
        "p90": pa.Column(float, nullable=True, coerce=True),
    },
    checks=[
        pa.Check(_interval_order, error="p10 <= p50 <= p90 violated"),
        pa.Check(
            lambda df: df["target_time"] == df["issue_time"] + pd.to_timedelta(df["horizon"], "h"),
            error="target_time != issue_time + horizon h",
        ),
    ],
    unique=["issue_time", "model_name", "horizon"],
    strict=True,
    name="Predictions",
)
