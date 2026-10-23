"""Feature schema v1: the ordered column list every model and contract uses (BUILD_PLAN §4.5).

Bump FEATURE_SCHEMA_VERSION whenever a column is added, removed, renamed, reordered or its
definition changes; a champion trained on another version is refused at predict time.
"""

from __future__ import annotations

from gridcast.features.calendar import CALENDAR_COLUMNS
from gridcast.weather import VARIABLES, WEATHER_COLUMNS

FEATURE_SCHEMA_VERSION = 1

# Issue-level demand features, read as of data_cutoff (one value per issue time).
# mkt_* : Market Demand, available target + REALTIME_LAG_H (RealtimeTotals live, PUB in training).
# ont_* : Ontario Demand, available under the PUB_Demand rule (D+1 13:00Z), so 8-32 h old.
ISSUE_COLUMNS: tuple[str, ...] = (
    "mkt_l0",  # Market Demand at the fixed offset issue_time - REALTIME_LAG_H (the plan's L0)
    "mkt_l0_age_h",  # hours between issue_time and the L0 hour (REALTIME_LAG_H unless a fallback)
    "mkt_mean24",
    "mkt_max24",
    "mkt_min24",
    "mkt_mean168",
    "mkt_l0_ratio168",
    "ont_last",  # newest Ontario Demand hour available at the cutoff
    "ont_last_age_h",
    "ont_mean24",
    "ont_mkt_ratio",  # Ontario / Market at the ont_last hour (exports proxy)
)

# Target-level demand lags (as of the same cutoff).
TARGET_LAG_COLUMNS: tuple[str, ...] = ("ont_lag168", "ont_lag336", "mkt_lag24")

WEATHER_AGG_COLUMNS: tuple[str, ...] = (
    *(f"{v}_w" for v in VARIABLES),  # population-weighted Ontario aggregates
    "temperature_2m_mean",  # simple 6-city mean
    "hdd_w",
    "cdd_w",
    "temp_w_mean24",  # trailing 24 h mean of temperature_2m_w, ending at the target hour
    "temp_w_delta3",  # temperature_2m_w(target) - temperature_2m_w(target - 3 h)
)

FEATURE_COLUMNS: tuple[str, ...] = (
    "horizon",
    *CALENDAR_COLUMNS,
    *ISSUE_COLUMNS,
    *TARGET_LAG_COLUMNS,
    *WEATHER_COLUMNS,
    *WEATHER_AGG_COLUMNS,
)

# Bookkeeping columns carried next to the features (never fed to a model).
META_COLUMNS: tuple[str, ...] = (
    "issue_time",
    "target_time",
    "input_available_at",  # max available_at over every input row used for this feature row
    "l0_target_time",
    "weather_available_at",
)
