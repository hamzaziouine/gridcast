"""Calendar features at the target hour (BUILD_PLAN §3.3, §3.5, §4.5).

Every date fact (holiday, day of week, weekend, long weekend, bridge day, Christmas period) is taken
from the CIVIL date of the target hour (`timeutil.civil`), not its EST date: during EDT, civil
00:00-01:00 of a holiday is HE24 of the previous EST date. `hour_est` is the IESO hour (HE - 1).

Holidays: `holidays.country_holidays("CA", subdiv="ON")` (public category) plus the Civic Holiday
(first Monday of August), which the package files under its *optional* category; the plan counts it
as a holiday (§3.5). Easter Monday and Remembrance Day, also optional, are not used.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from functools import lru_cache

import holidays
import numpy as np
import pandas as pd

from gridcast.timeutil import EST, civil

DAYS_TO_HOLIDAY_CAP = 60

# Stable ids (0 = not a holiday). Observed days share the id of the holiday they stand in for.
HOLIDAY_IDS: dict[str, int] = {
    "New Year's Day": 1,
    "Family Day": 2,
    "Good Friday": 3,
    "Victoria Day": 4,
    "Canada Day": 5,
    "Civic Holiday": 6,
    "Labour Day": 7,
    "Thanksgiving Day": 8,
    "Christmas Day": 9,
    "Boxing Day": 10,
}

CALENDAR_COLUMNS: tuple[str, ...] = (
    "hour_est",
    "civil_hour",
    "dow",
    "is_weekend",
    "doy_sin",
    "doy_cos",
    "month",
    "is_holiday",
    "holiday_id",
    "is_long_weekend",
    "is_bridge_day",
    "xmas_period",
    "days_to_next_holiday",
)


def _holiday_id(name: str) -> int:
    # "Boxing Day; Christmas Day (observed)" -> the first named holiday.
    first = name.split(";")[0].replace(" (observed)", "").strip()
    return HOLIDAY_IDS.get(first, 99)


@lru_cache(maxsize=64)
def holidays_for_year(year: int) -> dict[date, int]:
    """Ontario holidays of one year -> holiday id."""
    out = {
        d: _holiday_id(n)
        for d, n in holidays.country_holidays("CA", subdiv="ON", years=year).items()
    }
    optional = holidays.country_holidays("CA", subdiv="ON", years=year, categories=("optional",))
    for d, name in optional.items():
        if "Civic Holiday" in name:
            out[d] = HOLIDAY_IDS["Civic Holiday"]
    return out


def holiday_id(d: date) -> int:
    return holidays_for_year(d.year).get(d, 0)


def _is_off(d: date) -> bool:
    return d.weekday() >= 5 or holiday_id(d) > 0


def is_long_weekend(d: date) -> bool:
    """Part of a 3+ day block of weekend/holiday days that contains a holiday."""
    if not _is_off(d):
        return False
    start = d
    while _is_off(start - timedelta(days=1)):
        start -= timedelta(days=1)
    end = d
    while _is_off(end + timedelta(days=1)):
        end += timedelta(days=1)
    n = (end - start).days + 1
    has_holiday = any(holiday_id(start + timedelta(days=k)) for k in range(n))
    return n >= 3 and has_holiday


def is_bridge_day(d: date) -> bool:
    """A single working day between a holiday and a weekend (e.g. Fri after a Thu holiday)."""
    if _is_off(d):
        return False
    prev, nxt = d - timedelta(days=1), d + timedelta(days=1)
    return _is_off(prev) and _is_off(nxt) and (holiday_id(prev) > 0 or holiday_id(nxt) > 0)


def days_to_next_holiday(d: date) -> int:
    for k in range(DAYS_TO_HOLIDAY_CAP):
        if holiday_id(d + timedelta(days=k)):
            return k
    return DAYS_TO_HOLIDAY_CAP


@lru_cache(maxsize=8192)
def _date_facts(d: date) -> tuple[int, int, float, float, int, int, int, int, int, int, int]:
    doy = d.timetuple().tm_yday
    ang = 2 * np.pi * (doy - 1) / 365.25
    hid = holiday_id(d)
    xmas = (d.month == 12 and d.day >= 24) or (d.month == 1 and d.day <= 2)
    return (
        d.weekday(),
        int(d.weekday() >= 5),
        float(np.sin(ang)),
        float(np.cos(ang)),
        d.month,
        int(hid > 0),
        hid,
        int(is_long_weekend(d)),
        int(is_bridge_day(d)),
        int(xmas),
        days_to_next_holiday(d),
    )


def calendar_features(target_times: pd.DatetimeIndex) -> pd.DataFrame:
    """Calendar features for each target hour (UTC interval starts), in CALENDAR_COLUMNS order."""
    uniq = pd.DatetimeIndex(target_times.unique())
    rows = []
    for ts in uniq:
        t: datetime = ts.to_pydatetime()
        cdate, chour = civil(t)
        hour_est = t.astimezone(EST).hour
        rows.append((hour_est, chour, *_date_facts(cdate)))
    table = pd.DataFrame(rows, index=uniq, columns=list(CALENDAR_COLUMNS))
    out = table.reindex(target_times)
    out.index = pd.RangeIndex(len(out))
    return out.astype("float64")
