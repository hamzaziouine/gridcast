"""C6 against real Postgres: build(issue_time) reads raw_demand_history and weather_hourly."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pandas as pd
import pytest
from sqlalchemy import Engine, text

from gridcast import db
from gridcast.features.build import FEATURE_COLUMNS, build
from gridcast.timeutil import utc_to_he
from gridcast.weather import WEATHER_COLUMNS
from tests.synthetic import raw_history, weather_frame

pytestmark = pytest.mark.integration

START = datetime(2024, 1, 1, tzinfo=UTC)
END = datetime(2024, 2, 1, tzinfo=UTC)


def load_synthetic(engine: Engine, start: datetime = START, end: datetime = END) -> None:
    raw = raw_history(start, end)
    he = [utc_to_he(t) for t in raw["target_time"].dt.to_pydatetime()]
    raw["local_date"] = [d for d, _ in he]
    raw["hour_ending"] = [h for _, h in he]
    raw["file_name"] = "PUB_Demand_2024_v001.csv"
    raw["fetched_at"] = pd.Timestamp("2026-10-23", tz=UTC)
    cols = [
        "target_time",
        "source",
        "file_name",
        "local_date",
        "hour_ending",
        "market_demand_mw",
        "ontario_demand_mw",
        "available_at",
        "available_at_kind",
        "fetched_at",
    ]
    w = weather_frame(start, end)
    w["source"] = "historical_forecast"
    w["available_at_kind"] = "modelled"
    wcols = ["valid_time", "available_at", "source", "available_at_kind", *WEATHER_COLUMNS]
    with engine.begin() as conn:
        conn.execute(
            text(
                f"INSERT INTO raw_demand_history ({', '.join(cols)})"  # noqa: S608
                f" VALUES ({', '.join(':' + c for c in cols)})"
            ),
            raw[cols].to_dict("records"),
        )
        conn.execute(
            text(
                f"INSERT INTO weather_hourly ({', '.join(wcols)})"  # noqa: S608
                f" VALUES ({', '.join(':' + c for c in wcols)})"
            ),
            w[wcols].to_dict("records"),
        )


@pytest.fixture(scope="module")
def engine(fresh_db_url: str) -> Engine:
    db.upgrade(fresh_db_url)
    eng = db.make_engine(fresh_db_url)
    load_synthetic(eng)
    return eng


def test_build_from_database(engine: Engine) -> None:
    issue = datetime(2024, 1, 25, 9, tzinfo=UTC)
    f = build(issue, engine)
    assert len(f) == 24
    assert f[list(FEATURE_COLUMNS)].shape[1] == len(FEATURE_COLUMNS)
    assert f[["mkt_l0", "ont_last", "ont_lag168", "temperature_2m_w"]].notna().all().all()
    assert (f["issue_time"] - f["l0_target_time"] == timedelta(hours=1)).all()
    assert (f["input_available_at"] <= pd.Timestamp(issue + timedelta(minutes=30))).all()
