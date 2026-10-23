"""C1 / BUILD_PLAN §7.1: fixed UTC-5 hour-ending model, civil() and the civil-zone tripwire."""

from __future__ import annotations

import ast
from datetime import UTC, date, datetime, timedelta, timezone
from pathlib import Path

import pytest

from gridcast import timeutil
from gridcast.timeutil import EST, civil, day_target_times, he_to_utc, utc_to_he

SRC = Path(timeutil.__file__).resolve().parent
CIVIL_ZONE = "America/" + "Toronto"  # split so this test file never trips a repo-wide grep


def z(*args: int) -> datetime:
    return datetime(*args, tzinfo=UTC)  # type: ignore[misc]


# ---------------------------------------------------------------- HE <-> UTC


@pytest.mark.parametrize(
    ("d", "he", "expected"),
    [
        (date(2026, 1, 10), 1, z(2026, 1, 10, 5)),  # plan §4.3 example
        (date(2026, 1, 10), 24, z(2026, 1, 11, 4)),
        (date(2026, 7, 15), 1, z(2026, 7, 15, 5)),  # summer: still UTC-5, no DST shift
        (date(2026, 7, 15), 24, z(2026, 7, 16, 4)),
        (date(2026, 7, 15), 13, z(2026, 7, 15, 17)),
        (date(2025, 12, 31), 24, z(2026, 1, 1, 4)),  # year boundary
        (date(2026, 1, 1), 1, z(2026, 1, 1, 5)),
        (date(2024, 2, 29), 24, z(2024, 3, 1, 4)),  # leap day
    ],
)
def test_he_to_utc(d: date, he: int, expected: datetime) -> None:
    assert he_to_utc(d, he) == expected
    assert utc_to_he(expected) == (d, he)


@pytest.mark.parametrize(
    ("ts", "expected"),
    [
        (z(2026, 1, 1, 3), (date(2025, 12, 31), 23)),
        (z(2026, 1, 1, 4), (date(2025, 12, 31), 24)),
        (z(2026, 1, 1, 5), (date(2026, 1, 1), 1)),
    ],
)
def test_utc_to_he_year_boundary(ts: datetime, expected: tuple[date, int]) -> None:
    assert utc_to_he(ts) == expected


def test_utc_to_he_accepts_any_offset() -> None:
    assert utc_to_he(datetime(2026, 1, 10, 0, tzinfo=EST)) == (date(2026, 1, 10), 1)
    eest = timezone(timedelta(hours=3))
    assert utc_to_he(datetime(2026, 1, 10, 8, tzinfo=eest)) == (date(2026, 1, 10), 1)


@pytest.mark.parametrize("he", [0, 25, -1])
def test_he_out_of_range(he: int) -> None:
    with pytest.raises(ValueError, match="hour_ending"):
        he_to_utc(date(2026, 1, 1), he)


def test_naive_rejected() -> None:
    with pytest.raises(ValueError, match="naive"):
        utc_to_he(datetime(2026, 1, 1, 5))  # noqa: DTZ001
    with pytest.raises(ValueError, match="naive"):
        civil(datetime(2026, 1, 1, 5))  # noqa: DTZ001


def test_utc_to_he_rejects_off_hour() -> None:
    with pytest.raises(ValueError, match="on the hour"):
        utc_to_he(z(2026, 1, 1, 5, 30))


# ---------------------------------------------------------------- DST days: always 24 rows

DST_DAYS = [date(2025, 3, 9), date(2025, 11, 2), date(2026, 3, 8), date(2026, 11, 1)]


@pytest.mark.parametrize("d", DST_DAYS)
def test_dst_days_have_exactly_24_hours(d: date) -> None:
    times = day_target_times(d)
    assert len(times) == 24
    assert len(set(times)) == 24
    assert times[0] == z(d.year, d.month, d.day, 5)
    assert all(b - a == timedelta(hours=1) for a, b in zip(times, times[1:], strict=False))
    assert [utc_to_he(t) for t in times] == [(d, he) for he in range(1, 25)]
    # consecutive EST days tile the UTC axis with no gap or overlap
    assert day_target_times(d + timedelta(days=1))[0] - times[-1] == timedelta(hours=1)


# ---------------------------------------------------------------- civil()


def test_civil_spring_forward_2026_03_08() -> None:
    # 02:00 EST -> 03:00 EDT at 07:00Z: civil hour 2 does not exist that day.
    assert civil(z(2026, 3, 8, 5)) == (date(2026, 3, 8), 0)
    assert civil(z(2026, 3, 8, 6)) == (date(2026, 3, 8), 1)
    assert civil(z(2026, 3, 8, 7)) == (date(2026, 3, 8), 3)
    hours = [civil(t)[1] for t in day_target_times(date(2026, 3, 8))]
    assert 2 not in hours
    # EST HE24 of 03-08 starts 23:00 EST = 04:00Z on 03-09 = 00:00 EDT on 03-09
    assert civil(he_to_utc(date(2026, 3, 8), 24)) == (date(2026, 3, 9), 0)


def test_civil_fall_back_2026_11_01() -> None:
    # 02:00 EDT -> 01:00 EST at 06:00Z: civil hour 1 occurs twice.
    assert civil(z(2026, 11, 1, 4)) == (date(2026, 11, 1), 0)
    assert civil(z(2026, 11, 1, 5)) == (date(2026, 11, 1), 1)
    assert civil(z(2026, 11, 1, 6)) == (date(2026, 11, 1), 1)
    assert civil(z(2026, 11, 1, 7)) == (date(2026, 11, 1), 2)
    # From 06:00Z on, civil time equals EST, so HE h maps to civil hour h - 1.
    assert civil(he_to_utc(date(2026, 11, 1), 24)) == (date(2026, 11, 1), 23)


def test_civil_date_crosses_est_date_on_a_holiday_during_edt() -> None:
    # Civic Holiday 2026-08-03. HE24 of EST date 2026-08-02 starts 2026-08-03 04:00Z, which is
    # civil 2026-08-03 00:00 EDT: its calendar facts must come from the holiday's civil date.
    t = he_to_utc(date(2026, 8, 2), 24)
    assert t == z(2026, 8, 3, 4)
    assert utc_to_he(t) == (date(2026, 8, 2), 24)
    assert civil(t) == (date(2026, 8, 3), 0)


def test_civil_equals_est_in_winter() -> None:
    # Family Day 2027-02-15: in EST the EST and civil dates always agree.
    for he, t in enumerate(day_target_times(date(2027, 2, 15)), start=1):
        assert civil(t) == (date(2027, 2, 15), he - 1)


# ---------------------------------------------------------------- tripwire


def test_civil_zone_only_inside_timeutil_civil() -> None:
    """BUILD_PLAN rule 2: the civil zone name appears in src/gridcast only inside timeutil.civil."""
    offenders = [
        str(p.relative_to(SRC))
        for p in SRC.rglob("*.py")
        if p.name != "timeutil.py" and CIVIL_ZONE in p.read_text(encoding="utf-8")
    ]
    assert offenders == [], f"{CIVIL_ZONE} used outside timeutil.civil: {offenders}"

    tu_path = SRC / "timeutil.py"
    source = tu_path.read_text(encoding="utf-8")
    tree = ast.parse(source)
    civil_fn = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "civil")
    assert civil_fn.end_lineno is not None
    allowed = range(civil_fn.lineno, civil_fn.end_lineno + 1)
    lines = [i for i, line in enumerate(source.splitlines(), start=1) if CIVIL_ZONE in line]
    assert lines, "civil() must use the civil zone"
    assert all(i in allowed for i in lines), f"{CIVIL_ZONE} outside civil(): lines {lines}"
