"""C5: pandera contracts pass on the fixtures and reject broken frames (no network, no DB)."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pandas as pd
import pandera.errors
import pytest

from gridcast import contracts
from gridcast.ingest import ieso_adequacy, ieso_demand, ieso_realtime, openmeteo
from gridcast.ingest.http import Fetched

FIXTURES = Path(__file__).parents[1] / "fixtures"
SCHEMA_ERRORS = (pandera.errors.SchemaError, pandera.errors.SchemaErrors)
FETCHED_AT = datetime(2026, 10, 23, 20, 30, tzinfo=UTC)


def _pub_rows() -> pd.DataFrame:
    parsed = ieso_demand.parse_pub_demand((FIXTURES / "ieso_pub_demand_sample.csv").read_text())
    return ieso_demand.to_raw_rows(
        parsed, file_name="PUB_Demand_2026_v295.csv", fetched_at=FETCHED_AT, mode="modelled"
    )


def _rt_rows() -> pd.DataFrame:
    name = "PUB_RealtimeTotals_2026102314.csv"
    hourly = ieso_realtime.parse_realtime_totals(
        (FIXTURES / "ieso_realtime_sample.csv").read_text(), name
    )
    return ieso_realtime.to_raw_rows(hourly, file_name=name, fetched_at=FETCHED_AT, mode="observed")


def test_raw_demand_accepts_both_sources() -> None:
    contracts.RawDemand.validate(_pub_rows())
    contracts.RawDemand.validate(_rt_rows())


@pytest.mark.parametrize(
    ("column", "value"),
    [
        ("ontario_demand_mw", 1_500.0),  # e.g. GW parsed as MW
        ("market_demand_mw", 60_000.0),
        ("hour_ending", 25),
        ("available_at_kind", "guessed"),
    ],
)
def test_raw_demand_rejects_bad_values(column: str, value: object) -> None:
    rows = _pub_rows()
    rows.loc[3, column] = value
    with pytest.raises(SCHEMA_ERRORS):
        contracts.RawDemand.validate(rows)


def test_raw_demand_rejects_wrong_target_time_and_future_stamps() -> None:
    rows = _pub_rows()
    rows.loc[0, "target_time"] = rows.loc[0, "target_time"] + pd.Timedelta(hours=1)
    with pytest.raises(SCHEMA_ERRORS):
        contracts.RawDemand.validate(rows)
    rows = _pub_rows()
    rows.loc[0, "available_at"] = pd.Timestamp(FETCHED_AT + timedelta(hours=1))
    with pytest.raises(SCHEMA_ERRORS):
        contracts.RawDemand.validate(rows)


def test_raw_demand_rejects_realtime_with_ontario_demand() -> None:
    rows = _rt_rows()
    rows["ontario_demand_mw"] = 15_000.0
    with pytest.raises(SCHEMA_ERRORS):
        contracts.RawDemand.validate(rows)


def test_weather_hourly_contract() -> None:
    payload = json.loads((FIXTURES / "openmeteo_historical_sample.json").read_text())
    rows = openmeteo.to_rows(openmeteo.parse_response(payload), FETCHED_AT)
    contracts.WeatherHourly.validate(rows)
    bad = rows.copy()
    bad.loc[5, "relative_humidity_2m_ottawa"] = 140.0
    with pytest.raises(SCHEMA_ERRORS):
        contracts.WeatherHourly.validate(bad)
    bad = rows.copy()
    bad.loc[5, "available_at"] = bad.loc[5, "valid_time"]  # not the synthetic -24 h stamp
    with pytest.raises(SCHEMA_ERRORS):
        contracts.WeatherHourly.validate(bad)


def test_live_weather_snapshot_span() -> None:
    payload = json.loads((FIXTURES / "openmeteo_live_sample.json").read_text())
    frame = openmeteo.parse_response(payload)
    fetched_at = datetime(2026, 10, 23, 20, 27, 59, tzinfo=UTC)
    contracts.validate_weather_snapshot(frame, fetched_at)
    with pytest.raises(ValueError, match="span"):
        contracts.validate_weather_snapshot(frame, fetched_at + timedelta(hours=2))
    with pytest.raises(ValueError, match="span"):
        contracts.validate_weather_snapshot(frame.drop(index=30), fetched_at)


def test_fetch_live_runs_the_contract() -> None:
    body = (FIXTURES / "openmeteo_live_sample.json").read_text()
    late = datetime(2026, 10, 24, 6, tzinfo=UTC)  # a stale body for this fetch time

    def fake(url: str, params: dict[str, str]) -> Fetched:
        assert params["models"] == "gfs_seamless"
        assert (params["past_hours"], params["forecast_hours"]) == ("24", "48")
        return Fetched(url, 200, body, late, None)

    with pytest.raises(ValueError, match="span"):
        openmeteo.fetch_live(fake)


def test_ieso_snapshot_contract() -> None:
    snap = ieso_adequacy.parse_adequacy((FIXTURES / "ieso_adequacy_sample.xml").read_bytes())
    contracts.validate_ieso_snapshot(snap.created_at, snap.rows, FETCHED_AT)
    with pytest.raises(ValueError, match="after fetched_at"):
        contracts.validate_ieso_snapshot(
            snap.created_at, snap.rows, snap.created_at - timedelta(hours=1)
        )
    with pytest.raises(SCHEMA_ERRORS):
        contracts.validate_ieso_snapshot(snap.created_at, snap.rows.iloc[:23], FETCHED_AT)
    contracts.check_newest_snapshot(snap.created_at, FETCHED_AT)
    with pytest.raises(ValueError, match="3 days"):
        contracts.check_newest_snapshot(snap.created_at, FETCHED_AT + timedelta(days=4))
