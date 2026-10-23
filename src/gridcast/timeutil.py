"""Time model for IESO data (ADR-0002).

- IESO market time is EST, a fixed UTC-5 offset all year (no DST).
- Hours are hour-ending 1..24: HE1 of EST date D is the interval D 00:00-01:00 EST.
- `target_time` is always the interval START in UTC: HE h of date D -> D 00:00Z + (h - 1) h + 5 h,
  so HE1 -> D 05:00Z and HE24 -> D+1 04:00Z.
- The civil (wall-clock) zone is used in exactly one place, `civil()`, in the UTC -> local direction
  only. Every other function here, and every other module, uses the fixed offset. A unit test
  enforces this (tests/unit/test_timeutil.py, tripwire).
"""

from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

EST = timezone(timedelta(hours=-5), "EST")
HOUR = timedelta(hours=1)


def require_aware(ts: datetime) -> datetime:
    """Reject naive datetimes: every timestamp in GridCast carries its offset."""
    if ts.tzinfo is None or ts.utcoffset() is None:
        raise ValueError(f"naive datetime not allowed: {ts!r}")
    return ts


def to_utc(ts: datetime) -> datetime:
    return require_aware(ts).astimezone(UTC)


def he_to_utc(local_date: date, hour_ending: int) -> datetime:
    """IESO (EST date, hour-ending) -> interval start in UTC."""
    if not 1 <= hour_ending <= 24:
        raise ValueError(f"hour_ending must be 1..24, got {hour_ending}")
    start_est = datetime.combine(local_date, time(0), tzinfo=EST) + (hour_ending - 1) * HOUR
    return start_est.astimezone(UTC)


def utc_to_he(target_time: datetime) -> tuple[date, int]:
    """Interval start (any aware datetime on the hour) -> IESO (EST date, hour-ending)."""
    local = require_aware(target_time).astimezone(EST)
    if (local.minute, local.second, local.microsecond) != (0, 0, 0):
        raise ValueError(f"target_time must be on the hour: {target_time!r}")
    return local.date(), local.hour + 1


def est_date(ts: datetime) -> date:
    """The EST (market) date that contains `ts`."""
    return require_aware(ts).astimezone(EST).date()


def day_target_times(local_date: date) -> list[datetime]:
    """The 24 interval starts (UTC) of an EST date, HE1..HE24. Always 24: EST has no DST."""
    return [he_to_utc(local_date, he) for he in range(1, 25)]


def floor_hour(ts: datetime) -> datetime:
    """Floor to the top of the hour, in UTC."""
    return to_utc(ts).replace(minute=0, second=0, microsecond=0)


def civil(ts_utc: datetime) -> tuple[date, int]:
    """UTC instant -> (civil_date, civil_hour) on the Toronto wall clock (EST/EDT).

    The ONLY permitted use of the civil zone (BUILD_PLAN rule 2). Calendar date facts (holidays,
    day of week, weekends, long weekends) are computed from `civil_date`: during EDT, civil
    00:00-01:00 of a holiday is HE24 of the previous EST date.
    """
    local = to_utc(ts_utc).astimezone(ZoneInfo("America/Toronto"))
    return local.date(), local.hour
