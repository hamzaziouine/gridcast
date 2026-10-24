"""C11 against real Postgres + moto: PUB ingest -> scores -> peak_hours -> score_daily -> drift."""

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
from gridcast.jobs import daily, predict, publish
from gridcast.models import lgbm as lgbm_mod
from gridcast.storage.objstore import ObjectStore
from tests.integration.helpers import (
    Clock,
    ieso_fetch,
    insert_ieso_snapshot,
    install_champion,
    make_buckets,
    pub_demand_csv,
)
from tests.integration.test_features_db import START, load_synthetic

pytestmark = pytest.mark.integration

END = datetime(2024, 2, 3, tzinfo=UTC)
ISSUES = [datetime(2024, 1, 30, 5, tzinfo=UTC) + timedelta(hours=6 * k) for k in range(4)]
DAY = date(2024, 2, 1)  # the run is for Feb 1 (EST): it scores targets before Feb 1 05:00Z
JAN31 = [datetime(2024, 1, 31, 5, tzinfo=UTC) + timedelta(hours=k) for k in range(24)]


@pytest.fixture(scope="module")
def env(fresh_db_url: str) -> Iterator[tuple[Engine, ObjectStore, ObjectStore]]:
    db.upgrade(fresh_db_url)
    eng = db.make_engine(fresh_db_url)
    load_synthetic(eng, START, END)
    with eng.begin() as conn:
        targets = [
            r[0] for r in conn.execute(text("SELECT DISTINCT target_time FROM raw_demand_history"))
        ]
        rebuild_actuals(conn, targets)
    insert_ieso_snapshot(eng, ISSUES[0] - timedelta(hours=2), ISSUES[0], 48)
    with eng.begin() as conn:  # the champion's backtest MAPE stands in as the drift reference
        conn.execute(
            text(
                "INSERT INTO model_registry_events (event_type, model_name, model_version,"
                " metrics, reason, triggered_by) VALUES ('backtested', 'lgbm', 'lgbm-test',"
                " '{\"lgbm_mape\": 2.6}', 'test', 'manual')"
            )
        )
    with pytest.MonkeyPatch.context() as mp, mock_aws():
        mp.setattr(lgbm_mod, "MAX_ROUNDS", 5)
        mp.delenv("HEALTHCHECKS_DAILY_URL", raising=False)
        private, public = make_buckets()
        install_champion(eng, private, START, datetime(2024, 2, 1, tzinfo=UTC))
        for issue in ISSUES:
            predict.run(eng, private, issue, weather="store", run_kind="scheduled")
        yield eng, private, public
    eng.dispose()


def _revised(engine: Engine, delta: float) -> str:
    with engine.connect() as conn:
        cur = pd.read_sql(
            text("SELECT target_time, ontario_demand_mw::float8 AS v FROM actuals"
                 " WHERE target_time = ANY(:t) ORDER BY 1"),
            conn,
            params={"t": JAN31},
        )  # fmt: skip
    return pub_demand_csv(pd.Series(cur["v"].to_numpy() + delta, index=cur["target_time"]))


def _run(env: Any, day: date, body: str, version: int) -> dict[str, Any]:
    eng, private, public = env
    name = f"PUB_Demand_2024_v{version:03d}.csv"
    deps = daily.Deps(
        fetch=ieso_fetch(lambda: (name, body)),
        now=Clock(daily.day_start(day) + timedelta(hours=8, minutes=20)),
    )
    return daily.run(eng, private, public, deps, day=day)


def test_daily_scores_rollups_drift_and_scoreboard(env: Any) -> None:
    eng, _, public = env
    out = _run(env, DAY, _revised(eng, 7), 2)  # v001 = the synthetic load
    assert out["status"] == "ok", out
    assert out["pub_demand_rows"] == 24, out["notes"]  # Jan 31 revised -> 24 new versions
    with eng.connect() as conn:
        expected = conn.execute(
            text("SELECT count(*) FROM v_predictions WHERE target_time < :hi"),
            {"hi": daily.day_start(DAY)},
        ).scalar_one()
        n_scores = conn.execute(text("SELECT count(*) FROM scores")).scalar_one()
        revs = conn.execute(
            text("SELECT DISTINCT actual_revision FROM scores WHERE target_time = ANY(:t)"),
            {"t": JAN31},
        ).all()
        peaks = conn.execute(
            text("SELECT count(*) FROM peak_hours WHERE target_time = ANY(:t)"), {"t": JAN31}
        ).scalar_one()
        sd = conn.execute(
            text(
                "SELECT score_date, n FROM score_daily WHERE model_name = 'champion'"
                " AND horizon_band = 'all' ORDER BY 1"
            )
        ).all()
        drift = {r[0] for r in conn.execute(text("SELECT metric FROM drift_metrics"))}
    assert out["scores"]["inserted"] == n_scores == expected > 0
    assert revs == [(2,)]  # scored against the revised actual
    assert peaks == 24  # Jan 1..30 complete -> every Jan 31 target has a peak label
    assert [str(r[0]) for r in sd] == ["2024-01-30", "2024-01-31"]
    assert {"rolling_mape_7d", "ref_mape_30d", "naive_mape_7d", "coverage_30d",
            "neon_storage_mb"} <= drift  # fmt: skip

    sb = json.loads(public.get_bytes(publish.SCOREBOARD_KEY))
    assert sb["schema_version"] == 1 and sb["scored_through"] == "2024-01-31"
    champ = sb["windows"]["7d"]["models"]["champion"]
    assert set(champ) == {"n", "mape", "mae", "rmse", "coverage_80", "peak_mape", "n_peak"}
    assert champ["n"] > 0 and champ["mape"] is not None
    assert sb["windows"]["7d"]["models"]["seasonal_naive_168"]["n"] == champ["n"]  # paired
    assert {r["horizon"] for r in sb["by_horizon"]["30d"]["champion"]} <= set(range(1, 25))
    assert sb["daily"][-1]["date"] == "2024-01-31" and "champion" in sb["daily"][-1]


def test_rerun_is_skipped_and_revisions_are_rescored(env: Any) -> None:
    eng = env[0]
    again = _run(env, DAY, _revised(eng, 0), 2)
    assert again["status"] == "skipped"
    out = _run(env, DAY + timedelta(days=1), _revised(eng, 5), 3)  # one more revision of Jan 31
    assert out["status"] == "ok", out
    assert out["scores"]["rescored"] > 0
    with eng.connect() as conn:
        revs = conn.execute(
            text("SELECT DISTINCT actual_revision FROM scores WHERE target_time = ANY(:t)"),
            {"t": JAN31},
        ).all()
        statuses = conn.execute(
            text("SELECT status FROM pipeline_runs WHERE job = 'daily' ORDER BY started_at")
        ).all()
    assert revs == [(3,)]
    assert [s[0] for s in statuses] == ["ok", "skipped", "ok"]
