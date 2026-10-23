"""Publication-lag model: when could we first have seen a row? (ADR-0004, BUILD_PLAN §4.6)

Live ingest stamps `available_at_kind='observed'` with our own fetch time and does not use this
module. Backfilled rows get a `modelled` stamp from one function per source below. Every as-of
filter is `available_at <= data_cutoff`, with `data_cutoff = issue_time + 30 min`.
"""

from __future__ import annotations

from datetime import UTC, datetime, time, timedelta

from gridcast.config import DATA_CUTOFF_OFFSET, REALTIME_LAG_H
from gridcast.timeutil import HOUR, est_date, floor_hour, require_aware, to_utc

# PUB_Demand: every hour of EST date D is available at D+1 08:00 EST = D+1 13:00Z
# (the ~07:30 EST posting plus a 30-minute margin).
PUB_DEMAND_RELEASE_UTC = time(13, 0)

# Open-Meteo historical-forecast rows: synthetic lead of 24 h (optimistic by design, R11/S4).
HISTORICAL_FORECAST_LEAD = timedelta(hours=24)


def data_cutoff(issue_time: datetime) -> datetime:
    """The single as-of cutoff for an issue time (issue_time must be on the hour)."""
    issue = to_utc(issue_time)
    if issue != floor_hour(issue):
        raise ValueError(f"issue_time must be on the hour: {issue_time!r}")
    return issue + DATA_CUTOFF_OFFSET


def pub_demand_available_at(target_time: datetime) -> datetime:
    """Modelled availability of a PUB_Demand hour: D+1 13:00Z for every hour of EST date D."""
    d = est_date(target_time)
    return datetime.combine(d + timedelta(days=1), PUB_DEMAND_RELEASE_UTC, tzinfo=UTC)


def realtime_totals_available_at(target_time: datetime, lag_h: int = REALTIME_LAG_H) -> datetime:
    """Modelled availability of a RealtimeTotals hour: target_time + REALTIME_LAG_H."""
    return to_utc(target_time) + lag_h * HOUR


def market_demand_available_at(target_time: datetime, lag_h: int = REALTIME_LAG_H) -> datetime:
    """Modelled availability of a Market Demand value, whichever file it came from.

    Plan §3.0 decision 1: RealtimeTotals supplies Market Demand live; in training the PUB_Demand
    `Market Demand` column stands in for it (the series are identical) and gets the same stamp,
    not the PUB_Demand D+1 13:00Z rule that applies to its Ontario Demand.
    """
    return realtime_totals_available_at(target_time, lag_h)


def historical_forecast_available_at(valid_time: datetime) -> datetime:
    """Synthetic availability of an archived Open-Meteo forecast row: valid_time - 24 h."""
    return to_utc(valid_time) - HISTORICAL_FORECAST_LEAD


def live_snapshot_available_at(fetched_at: datetime) -> datetime:
    """A live weather snapshot is available when we fetched it."""
    return to_utc(require_aware(fetched_at))


def latest_realtime_target(issue_time: datetime, lag_h: int = REALTIME_LAG_H) -> datetime:
    """Newest RealtimeTotals target_time whose modelled stamp is <= data_cutoff(issue_time).

    This is the L0 hour the feature builder may use; `issue_time - result` is the modelled L0 lag
    and must equal `lag_h` for every issue hour (lag parity, §4.6).
    """
    cutoff = data_cutoff(issue_time)
    return floor_hour(cutoff - lag_h * HOUR)
