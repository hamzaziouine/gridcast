"""C12 end-to-end: one synthetic EST day of hourly runs, then the next morning's daily run.

Real Postgres (pgserver / CI service container) + moto S3; IESO and Open-Meteo are fakes (no
network). Asserts the predictions written hour by hour (with one missed hour back-filled by
catch-up), the scores the daily job derives once the day's actuals land, and the shape of the
three published JSON documents.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from datetime import UTC, date, datetime, timedelta
from typing import Any

import pandas as pd
import pytest
from moto import mock_aws
from sqlalchemy import Engine, text

from gridcast import db
from gridcast.ingest.ieso_demand import rebuild_actuals
from gridcast.jobs import daily, hourly, publish
from gridcast.models import lgbm as lgbm_mod
from gridcast.storage.objstore import ObjectStore
from tests.integration.helpers import (
    Clock,
    ieso_fetch,
    insert_ieso_snapshot,
    install_champion,
    make_buckets,
    pub_demand_csv,
    snapshot_fetcher,
)
from tests.integration.test_features_db import START, load_synthetic
from tests.synthetic import hours, ontario_series

pytestmark = pytest.mark.integration

DAY_START = datetime(2024, 1, 30, 5, tzinfo=UTC)  # HE1 of EST day 2024-01-30
ISSUES = [DAY_START + timedelta(hours=k) for k in range(24)]
MISSED = ISSUES[7]  # the cron never ran for this hour; the next run back-fills it
DAILY_DAY = date(2024, 1, 31)  # the 08:20 EST run on Jan 31 scores targets before 05:00Z


@pytest.fixture(scope="module")
def env(fresh_db_url: str) -> Iterator[tuple[Engine, ObjectStore, ObjectStore]]:
    db.upgrade(fresh_db_url)
    eng = db.make_engine(fresh_db_url)
    load_synthetic(eng, START, DAY_START)  # history known before the day starts
    with eng.begin() as conn:
        targets = [r[0] for r in conn.execute(text("SELECT target_time FROM raw_demand_history"))]
        rebuild_actuals(conn, targets)
    insert_ieso_snapshot(eng, DAY_START - timedelta(hours=6), DAY_START - timedelta(hours=6), 60)
    with pytest.MonkeyPatch.context() as mp, mock_aws():
        mp.setattr(lgbm_mod, "MAX_ROUNDS", 5)
        for name in ("HEALTHCHECKS_HOURLY_URL", "HEALTHCHECKS_DAILY_URL"):
            mp.delenv(name, raising=False)
        private, public = make_buckets()
        install_champion(eng, private, START, DAY_START)
        yield eng, private, public
    eng.dispose()


def _day_values() -> pd.Series:
    """The final Ontario Demand of the day, as the next morning's PUB_Demand file carries it."""
    idx = hours(START, DAY_START + timedelta(days=1))
    return pd.Series(ontario_series(idx), index=idx)[DAY_START:]


def test_hourly_day_then_daily(env: Any) -> None:
    eng, private, public = env
    statuses = []
    for issue in ISSUES:
        if issue == MISSED:
            continue
        clock = Clock(issue + timedelta(minutes=7))
        deps = hourly.Deps(fetch=ieso_fetch(), fetch_snapshot=snapshot_fetcher(clock), now=clock)
        out = hourly.run(eng, private, public, deps)
        statuses.append(out["status"])
        if issue == MISSED + timedelta(hours=1):
            assert MISSED.isoformat() in out["catchup"]
    assert statuses == ["ok"] * 23

    with eng.connect() as conn:
        champ = pd.read_sql(
            text(
                "SELECT issue_time, run_kind, count(*) AS n FROM v_predictions"
                " WHERE model_name = 'champion' GROUP BY 1, 2 ORDER BY 1"
            ),
            conn,
        )
        n_pred = conn.execute(text("SELECT count(*) FROM predictions")).scalar_one()
    assert len(champ) == 24 and (champ["n"] == 24).all()
    kinds = dict(zip(pd.to_datetime(champ["issue_time"], utc=True), champ["run_kind"], strict=True))
    assert kinds[pd.Timestamp(MISSED)] == "catchup"
    assert {kinds[pd.Timestamp(i)] for i in ISSUES if i != MISSED} == {"scheduled"}

    fc = json.loads(public.get_bytes(publish.FORECAST_KEY))
    assert set(fc) == {
        "schema_version", "issue_time", "data_cutoff", "model_version", "run_kind", "units",
        "target", "rows", "attribution",
    }  # fmt: skip
    assert fc["issue_time"] == "2024-01-31T04:00:00Z" and len(fc["rows"]) == 24
    assert all(r["p10"] <= r["p50"] <= r["p90"] for r in fc["rows"])
    meta = json.loads(public.get_bytes(publish.META_KEY))
    assert set(meta) == {
        "schema_version", "generated_at", "last_issue_time", "stale_after_minutes", "champion",
        "last_run", "inputs", "keys", "attribution",
    }  # fmt: skip
    assert meta["last_run"]["status"] == "ok"

    # Next morning: the day's final actuals land, the daily job scores everything.
    body = pub_demand_csv(_day_values(), created="2024-01-31 07:30:09")
    clock = Clock(daily.day_start(DAILY_DAY) + timedelta(hours=8, minutes=20))
    deps_d = daily.Deps(fetch=ieso_fetch(lambda: ("PUB_Demand_2024_v002.csv", body)), now=clock)
    out = daily.run(eng, private, public, deps_d)
    assert out["status"] == "ok", out
    assert out["pub_demand_rows"] == 24

    with eng.connect() as conn:
        scorable = conn.execute(
            text(
                "SELECT count(*) FROM v_predictions p JOIN actuals a USING (target_time)"
                " WHERE p.target_time < :hi"
            ),
            {"hi": daily.day_start(DAILY_DAY)},
        ).scalar_one()
        per_model = dict(
            conn.execute(
                text(
                    "SELECT model_name, count(*) FROM v_scores WHERE target_time >= :lo GROUP BY 1"
                ),
                {"lo": DAY_START},
            ).all()
        )
        sd_dates = [
            str(r[0])
            for r in conn.execute(
                text(
                    "SELECT DISTINCT score_date FROM score_daily WHERE model_name = 'champion'"
                    " ORDER BY 1"
                )
            )
        ]
    assert out["scores"]["inserted"] == scorable > 0
    assert n_pred >= scorable
    # Target DAY_START + j h has one champion forecast per issue hour DAY_START .. target - 1 h.
    assert per_model["champion"] == sum(range(24))
    assert set(per_model) >= {"champion", "seasonal_naive_168", "persistence", "ieso_adequacy"}
    assert sd_dates == ["2024-01-30"]

    sb = json.loads(public.get_bytes(publish.SCOREBOARD_KEY))
    assert set(sb) == {
        "schema_version", "generated_at", "scored_through", "champion", "pairing", "windows",
        "by_horizon", "daily", "attribution",
    }  # fmt: skip
    assert set(sb["windows"]) == {"7d", "30d", "since_launch"}
    assert sb["scored_through"] == "2024-01-30"
    assert sb["windows"]["7d"]["models"]["champion"]["n"] == per_model["champion"]
    horizons = [r["horizon"] for r in sb["by_horizon"]["30d"]["champion"]]
    assert horizons == list(range(1, 24))  # h = 24 from DAY_START lands on the next EST day
