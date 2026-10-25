"""C15 against real Postgres + moto: `gridcast status build` reads v_predictions / actuals and
writes the status-branch files, mirroring the public snapshots it can read."""

from __future__ import annotations

import json
import uuid
import xml.etree.ElementTree as ET  # noqa: S405 - our own output
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
import pytest
from moto import mock_aws
from sqlalchemy import text

from gridcast import db
from gridcast.jobs import predict, publish
from gridcast.status import build
from tests.integration.helpers import make_buckets

pytestmark = pytest.mark.integration

NOW = datetime(2026, 11, 3, 14, 10, tzinfo=UTC)
H0 = NOW.replace(minute=0)


@pytest.fixture(scope="module")
def env(fresh_db_url: str) -> Iterator[Any]:
    db.upgrade(fresh_db_url)
    eng = db.make_engine(fresh_db_url)
    targets = pd.date_range(H0 - timedelta(days=8), H0 - timedelta(hours=30), freq="h")
    with eng.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO actuals (target_time, ontario_demand_mw, source, available_at,"
                " available_at_kind) VALUES (:t, 15000, 'pub_demand', :t, 'modelled')"
            ),
            [{"t": t.to_pydatetime()} for t in targets],
        )
    for issue in pd.date_range(H0 - timedelta(days=3), H0, freq="h"):
        rows = []
        for name, bias in (("champion", 150.0), ("ieso_adequacy", 300.0),
                           ("seasonal_naive_168", 900.0), ("persistence", 2000.0)):  # fmt: skip
            for h in range(1, 25):
                p50 = 15000.0 + bias + h
                band = name != "ieso_adequacy"
                rows.append({
                    "issue_time": issue, "target_time": issue + pd.Timedelta(hours=h),
                    "horizon": h, "model_name": name,
                    "model_version": issue.isoformat() if name == "ieso_adequacy" else f"{name}-v",
                    "p10": p50 - 400 if band else np.nan, "p50": p50,
                    "p90": p50 + 400 if band else np.nan,
                })  # fmt: skip
        predict.write_predictions(eng, pd.DataFrame(rows), uuid.uuid4(), "scheduled")
    with mock_aws():
        private, public = make_buckets()
        yield eng, private, public
    eng.dispose()


def test_status_build_writes_chart_metrics_and_mirror(env: Any, tmp_path: Path) -> None:
    eng, private, public = env
    private.put_json(predict.CHAMPION_KEY, {"model_name": "lgbm", "model_version": "champion-v",
                                            "champion_status": "ok"})  # fmt: skip
    publish.put(public, publish.FORECAST_KEY, {"issue_time": "x"})
    res = build.build(eng, private, public, tmp_path, NOW)
    assert res["mirror_missing"] == [publish.META_KEY, publish.SCOREBOARD_KEY]
    assert set(res["files"]) == {"chart_7d.svg", "metrics.json", "README.md",
                                 publish.FORECAST_KEY}  # fmt: skip
    chart = res["chart"]
    assert chart["horizon_h"] == 24 and chart["n_scored_hours"] == 19
    assert chart["best_baseline"] == "seasonal_naive_168"  # ieso is not a baseline
    assert chart["mape_pct"]["champion"] == pytest.approx(100 * 174 / 15000, abs=1e-3)
    svg = (tmp_path / "chart_7d.svg").read_text(encoding="utf-8")
    ET.fromstring(svg)  # noqa: S314
    assert "model champion-v" in svg and "github.com/hamzaziouine/gridcast" in svg
    metrics = json.loads((tmp_path / "metrics.json").read_text(encoding="utf-8"))
    assert metrics["champion"]["model_version"] == "champion-v"
    assert metrics["urls"]["api"].startswith("https://")
    assert (tmp_path / publish.FORECAST_KEY).read_bytes() == public.get_bytes(publish.FORECAST_KEY)
