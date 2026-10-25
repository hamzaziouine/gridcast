"""C16 `prune` against real Postgres: dry-run deletes nothing; apply deletes exactly the plan's
rows (weak names > 90 days with their scores, old realtime versions), keeps everything else."""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import Any

import numpy as np
import pandas as pd
import pytest
from sqlalchemy import Engine, text

from gridcast import db
from gridcast.jobs import daily, predict, prune

pytestmark = pytest.mark.integration

NOW = datetime(2027, 3, 1, 5, 7, tzinfo=UTC)
OLD = NOW.replace(minute=0) - timedelta(days=prune.PRUNE_DAYS) - timedelta(hours=1)  # pruned
NEW = NOW.replace(minute=0) - timedelta(days=prune.PRUNE_DAYS) + timedelta(hours=1)  # kept
NAMES = ("champion", "seasonal_naive_168", "seasonal_naive_24", "persistence", "prev_champion",
         "ieso_adequacy")  # fmt: skip


def _count(engine: Engine, sql: str, **p: Any) -> int:
    with engine.connect() as conn:
        return int(conn.execute(text(sql), p).scalar_one())


@pytest.fixture(scope="module")
def eng(fresh_db_url: str) -> Iterator[Engine]:
    db.upgrade(fresh_db_url)
    e = db.make_engine(fresh_db_url)
    for issue in (OLD, NEW):
        rows = [
            {"issue_time": issue, "target_time": issue + timedelta(hours=h), "horizon": h,
             "model_name": n, "model_version": issue.isoformat() if n == "ieso_adequacy" else n,
             "p10": np.nan, "p50": 15000.0, "p90": np.nan}
            for n in NAMES for h in range(1, 25)
        ]  # fmt: skip
        predict.write_predictions(e, pd.DataFrame(rows), uuid.uuid4(), "scheduled")
    with e.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO scores (target_time, model_id, horizon, actual_revision, actual_mw)"
                " SELECT issue_time + horizon * interval '1 hour', model_id, horizon, 1, 15100"
                " FROM predictions"
            )
        )
        rt = NOW.replace(minute=0) - timedelta(days=prune.REALTIME_DAYS)
        for t, versions in ((rt - timedelta(hours=2), 3), (rt + timedelta(hours=2), 2)):
            for v in range(versions):
                conn.execute(
                    text(
                        "INSERT INTO raw_demand_history (target_time, source, file_name,"
                        " local_date, hour_ending, market_demand_mw, n_intervals, available_at,"
                        " available_at_kind, fetched_at)"
                        " VALUES (:t, :s, :f, :d, 1, 17000, 12, :a, 'observed', :a)"
                    ),
                    {"t": t, "s": "realtime_totals", "f": f"rt_v{v}.csv", "d": t.date(),
                     "a": t + timedelta(hours=1, minutes=v)},
                )  # fmt: skip
            conn.execute(
                text(
                    "INSERT INTO raw_demand_history (target_time, source, file_name, local_date,"
                    " hour_ending, ontario_demand_mw, available_at, available_at_kind, fetched_at)"
                    " VALUES (:t, 'pub_demand', 'PUB_Demand_2027.csv', :d, 1, 15000, :t,"
                    " 'modelled', :t)"
                ),
                {"t": t, "d": t.date()},
            )
    yield e
    e.dispose()


def test_retention_is_longer_than_the_score_daily_rebuild_window() -> None:
    assert prune.PRUNE_DAYS > daily.SCORE_DAYS  # aggregates of pruned rows are never recomputed


def test_dry_run_counts_and_deletes_nothing(eng: Engine) -> None:
    total = _count(eng, "SELECT count(*) FROM predictions")
    out = prune.run(eng, None, apply_changes=False, now=lambda: NOW, ping=lambda u: None)
    assert out["status"] == "skipped" and out["heartbeat"] == "dry-run"
    assert out["rows"] == {"scores": 72, "predictions": 72, "prediction_runs": 3,
                           "raw_demand_history_realtime": 2}  # fmt: skip
    assert _count(eng, "SELECT count(*) FROM predictions") == total
    assert out["before"]["database_bytes"] > 0


def test_apply_deletes_only_what_the_plan_allows(eng: Engine) -> None:
    pings: list[str] = []
    out = prune.run(eng, None, apply_changes=True, now=lambda: NOW, ping=pings.append)
    assert out["status"] == "ok", out
    assert out["deleted"] == {"scores": 72, "predictions": 72, "prediction_runs": 3,
                              "raw_demand_history_realtime": 2}  # fmt: skip
    q = "SELECT count(*) FROM v_predictions WHERE issue_time = :i AND model_name = :n"
    for n in NAMES:
        kept_old = _count(eng, q, i=OLD, n=n)
        assert kept_old == (0 if n in prune.WEAK_MODELS else 24), n
        assert _count(eng, q, i=NEW, n=n) == 24, n  # younger than 90 days: untouched
    # no orphan scores: every score row still joins its prediction
    assert _count(eng, "SELECT count(*) FROM scores") == _count(
        eng, "SELECT count(*) FROM v_scores"
    )
    # realtime: newest version of the old hour kept, recent hour and PUB rows untouched
    assert (
        _count(eng, "SELECT count(*) FROM raw_demand_history WHERE source = 'realtime_totals'") == 3
    )
    assert _count(eng, "SELECT count(*) FROM raw_demand_history WHERE file_name = 'rt_v2.csv'") == 1
    assert _count(eng, "SELECT count(*) FROM raw_demand_history WHERE source = 'pub_demand'") == 2
    again = prune.run(eng, None, apply_changes=True, now=lambda: NOW + timedelta(hours=1),
                      ping=pings.append)  # fmt: skip
    assert sum(again["deleted"].values()) == 0  # idempotent
    assert pings == []  # no HEALTHCHECKS_PRUNE_URL in tests: heartbeat disabled
