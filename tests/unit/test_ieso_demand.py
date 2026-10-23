"""PUB_Demand parser and version/availability rules (no network, no database)."""

from __future__ import annotations

from datetime import UTC, date, datetime
from pathlib import Path

import pandas as pd
import pytest

from gridcast.ingest import ieso_demand as d
from gridcast.ingest.http import EmptyFeedError

FIXTURE = Path(__file__).parents[1] / "fixtures" / "ieso_pub_demand_sample.csv"


def test_parse_fixture_skips_junk_lines() -> None:
    df = d.parse_pub_demand(FIXTURE.read_text())
    assert len(df) == 6 * 24 + 1
    first = df.iloc[0]
    assert first["local_date"] == date(2026, 10, 17)
    assert first["hour_ending"] == 1
    assert first["target_time"] == pd.Timestamp("2026-10-17 05:00", tz="UTC")
    last = df.iloc[-1]
    assert (last["market_demand_mw"], last["ontario_demand_mw"]) == (14664.0, 14021.0)
    assert last["target_time"] == pd.Timestamp("2026-10-23 05:00", tz="UTC")


def test_parse_tolerates_the_market_renewal_gap() -> None:
    body = (
        "\\Hourly Demand Report,,,\n\\Created at 2026-01-31 07:30:13,,,\n\\For 2025,,,\n"
        "Date,Hour,Market Demand,Ontario Demand\n"
        "2025-04-30,24,15320,13795\n2025-05-01,2,13153,12352\n"
    )
    df = d.parse_pub_demand(body)
    assert list(df["hour_ending"]) == [24, 2]
    gap = df["target_time"].diff().iloc[1]
    assert gap == pd.Timedelta(hours=2)


def test_parse_raises_on_empty_feed() -> None:
    with pytest.raises(EmptyFeedError):
        d.parse_pub_demand("\\Hourly Demand Report,,,\nDate,Hour,Market Demand,Ontario Demand\n")
    with pytest.raises(EmptyFeedError):
        d.parse_pub_demand("<html>not found</html>")


def test_parse_rejects_unexpected_header() -> None:
    with pytest.raises(ValueError, match="header"):
        d.parse_pub_demand("Date,Hour,Ontario Demand,Market Demand\n2025-01-01,1,1,2\n")


@pytest.mark.parametrize(
    ("name", "year", "version"),
    [
        ("PUB_Demand.csv", None, None),
        ("PUB_Demand_2025.csv", 2025, None),
        ("PUB_Demand_2026_v295.csv", 2026, "v295"),
        ("PUB_Demand_2015_v1.csv", 2015, "v001"),
    ],
)
def test_parse_file_name(name: str, year: int | None, version: str | None) -> None:
    assert d.parse_file_name(name) == (year, version)


def test_latest_files_by_year_prefers_highest_version() -> None:
    listing = [
        "PUB_Demand.csv",
        "PUB_Demand_2025.csv",
        "PUB_Demand_2025_v396.csv",
        "PUB_Demand_2026.csv",
        "PUB_Demand_2026_v293.csv",
        "PUB_Demand_2026_v295.csv",
        "PUB_Demand_2026_v294.csv",
        "PUB_Demand_2024.csv",
        "other.csv",
    ]
    assert d.latest_files_by_year(listing) == {
        2024: "PUB_Demand_2024.csv",
        2025: "PUB_Demand_2025_v396.csv",
        2026: "PUB_Demand_2026_v295.csv",
    }


def test_created_at_is_est() -> None:
    assert d.parse_created_at(FIXTURE.read_text()) == datetime(2026, 10, 23, 12, 30, 9, tzinfo=UTC)


def test_modelled_rows_drop_hours_not_yet_published() -> None:
    parsed = d.parse_pub_demand(FIXTURE.read_text())
    fetched_at = datetime(2026, 10, 23, 20, 0, tzinfo=UTC)
    rows = d.to_raw_rows(
        parsed, file_name="PUB_Demand_2026_v295.csv", fetched_at=fetched_at, mode="modelled"
    )
    # 2026-10-23 HE1 is modelled available at 2026-10-24 13:00Z, after the fetch: dropped.
    assert len(rows) == 6 * 24
    assert set(rows["available_at_kind"]) == {"modelled"}
    assert set(rows["file_version"]) == {"v295"}
    # Every hour of EST date 2026-10-22 is stamped 2026-10-23 13:00Z.
    day = rows[rows["local_date"] == date(2026, 10, 22)]
    assert set(day["available_at"]) == {pd.Timestamp("2026-10-23 13:00", tz="UTC")}


def test_observed_rows_use_fetch_time() -> None:
    parsed = d.parse_pub_demand(FIXTURE.read_text())
    fetched_at = datetime(2026, 10, 23, 13, 20, tzinfo=UTC)
    rows = d.to_raw_rows(parsed, file_name="PUB_Demand.csv", fetched_at=fetched_at, mode="observed")
    assert len(rows) == len(parsed)
    assert set(rows["available_at"]) == {pd.Timestamp(fetched_at)}
    assert rows["file_version"].isna().all()
    assert tuple(rows.columns) == d.RAW_COLUMNS


def test_week_ratio_outliers() -> None:
    idx = pd.date_range("2025-01-01", periods=24 * 28, freq="h", tz="UTC")
    s = pd.Series(15000.0, index=idx)
    s.iloc[24 * 3] = 4598.0  # week 1: no week-earlier value, so never flagged
    s.iloc[24 * 9] = 4598.0  # week 2: below 50 % of both neighbours
    s.iloc[-1] = 30000.0  # last hour: no week-later value, one-sided rule
    flagged = d.week_ratio_outliers(s)
    assert list(flagged.index) == [idx[24 * 9], idx[-1]]
