"""RealtimeTotals and Adequacy3 parsers (no network, no database)."""

from __future__ import annotations

from datetime import UTC, date, datetime
from pathlib import Path

import pandas as pd
import pytest

from gridcast.ingest import ieso_adequacy as a
from gridcast.ingest import ieso_realtime as r
from gridcast.ingest.http import EmptyFeedError

FIXTURES = Path(__file__).parents[1] / "fixtures"
COMPLETE = "PUB_RealtimeTotals_2026102314.csv"
PARTIAL = "PUB_RealtimeTotals_2026102311.csv"


def test_realtime_complete_hour_is_mean_total_energy() -> None:
    hourly = r.parse_realtime_totals((FIXTURES / "ieso_realtime_sample.csv").read_text(), COMPLETE)
    assert len(hourly) == 1
    row = hourly.iloc[0]
    assert (row["local_date"], row["hour_ending"], row["n_intervals"]) == (
        date(2026, 10, 23),
        14,
        12,
    )
    assert row["target_time"] == pd.Timestamp("2026-10-23 18:00", tz="UTC")
    assert row["market_demand_mw"] == 16170.21  # 194,042.5 / 12


def test_realtime_partial_hour_is_not_stored() -> None:
    body = (FIXTURES / "ieso_realtime_partial_sample.csv").read_text()
    hourly = r.parse_realtime_totals(body, PARTIAL)
    assert hourly.iloc[0]["n_intervals"] == 2
    rows = r.to_raw_rows(
        hourly,
        file_name=PARTIAL,
        fetched_at=datetime(2026, 10, 23, 20, tzinfo=UTC),
        mode="observed",
    )
    assert rows.empty


def test_realtime_handles_crlf_and_rows() -> None:
    body = (FIXTURES / "ieso_realtime_sample.csv").read_text().replace("\n", "\r\n")
    hourly = r.parse_realtime_totals(body, COMPLETE)
    fetched_at = datetime(2026, 10, 23, 19, 30, tzinfo=UTC)
    rows = r.to_raw_rows(hourly, file_name=COMPLETE, fetched_at=fetched_at, mode="observed")
    assert rows.iloc[0]["source"] == "realtime_totals"
    assert rows.iloc[0]["ontario_demand_mw"] is None
    assert rows.iloc[0]["available_at"] == pd.Timestamp(fetched_at)


def test_realtime_modelled_uses_lag() -> None:
    hourly = r.parse_realtime_totals((FIXTURES / "ieso_realtime_sample.csv").read_text(), COMPLETE)
    early = datetime(2026, 10, 23, 18, 30, tzinfo=UTC)  # before target + 1 h
    late = datetime(2026, 10, 23, 19, 0, tzinfo=UTC)
    kw = {"file_name": COMPLETE, "mode": "modelled", "lag_h": 1}
    assert r.to_raw_rows(hourly, fetched_at=early, **kw).empty  # type: ignore[arg-type]
    rows = r.to_raw_rows(hourly, fetched_at=late, **kw)  # type: ignore[arg-type]
    assert rows.iloc[0]["available_at"] == pd.Timestamp(late)


def test_realtime_rejects_mismatched_names() -> None:
    body = (FIXTURES / "ieso_realtime_sample.csv").read_text()
    with pytest.raises(ValueError, match="HOUR"):
        r.parse_realtime_totals(body, "PUB_RealtimeTotals_2026102315.csv")
    with pytest.raises(ValueError, match="FOR date"):
        r.parse_realtime_totals(body, "PUB_RealtimeTotals_2026102214.csv")
    with pytest.raises(EmptyFeedError):
        r.parse_realtime_totals("<html>404</html>", COMPLETE)


def test_realtime_file_name_for_target() -> None:
    assert r.file_name_for(datetime(2026, 10, 23, 18, tzinfo=UTC)) == COMPLETE
    assert r.file_name_for(datetime(2026, 10, 24, 4, tzinfo=UTC)) == (
        "PUB_RealtimeTotals_2026102324.csv"
    )


@pytest.mark.parametrize(
    ("created", "expected"),
    [
        ("2026-11-23T10:31:05", datetime(2026, 11, 23, 15, 31, 5, tzinfo=UTC)),
        # July: still UTC-5, no DST shift (DATA_NOTES §3.3).
        ("2026-07-24T23:48:50", datetime(2026, 7, 25, 4, 48, 50, tzinfo=UTC)),
    ],
)
def test_created_at_is_fixed_est(created: str, expected: datetime) -> None:
    assert a.parse_created_at(created) == expected


def test_adequacy_fixture() -> None:
    snap = a.parse_adequacy((FIXTURES / "ieso_adequacy_sample.xml").read_bytes())
    assert snap.created_at == datetime(2026, 10, 23, 17, 53, 16, tzinfo=UTC)
    assert snap.delivery_date == date(2026, 10, 24)
    assert len(snap.rows) == 24
    assert snap.rows["target_time"].iloc[0] == pd.Timestamp("2026-10-24 05:00", tz="UTC")
    assert list(snap.rows["energy_mw"].iloc[:3]) == [13983.0, 13634.0, 13592.0]
    assert snap.rows.set_index("target_time")["energy_mw"].iloc[18] == 16185.0  # HE19


def test_adequacy_without_namespace_finds_nothing() -> None:
    xml = (FIXTURES / "ieso_adequacy_sample.xml").read_text()
    xml = xml.replace('xmlns="http://www.ieso.ca/schema"', "")
    with pytest.raises(ValueError):
        a.parse_adequacy(xml.encode())


def test_pending_files_oldest_first() -> None:
    listing = [
        "PUB_Adequacy3.xml",
        "PUB_Adequacy3_20261024.xml",
        "PUB_Adequacy3_20261024_v10.xml",
        "PUB_Adequacy3_20261024_v9.xml",
        "PUB_Adequacy3_20261023_v97.xml",
        "PUB_Adequacy3_20261030_v1.xml",
    ]
    out = a.pending_files(
        listing, [date(2026, 10, 23), date(2026, 10, 24)], {"PUB_Adequacy3_20261023_v97.xml"}
    )
    assert out == ["PUB_Adequacy3_20261024_v9.xml", "PUB_Adequacy3_20261024_v10.xml"]


def test_archive_path(tmp_path: Path) -> None:
    p = a.archive_path(tmp_path, "PUB_Adequacy3_20261024_v68.xml", date(2026, 10, 24))
    assert p == tmp_path / "ieso/adequacy3/2026/10/24/PUB_Adequacy3_20261024_v68.xml.gz"
