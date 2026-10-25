"""C16 keepalive checks against real Postgres: stale hourly/daily, loop liveness once armed."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import Engine, text

from gridcast import db
from gridcast.jobs import keepalive, runs

pytestmark = pytest.mark.integration

NOW = datetime(2026, 11, 2, 6, 0, tzinfo=UTC)


@pytest.fixture()
def engine(fresh_db_url: str) -> Iterator[Engine]:
    db.upgrade(fresh_db_url)
    eng = db.make_engine(fresh_db_url)
    with eng.begin() as conn:
        conn.execute(text("TRUNCATE pipeline_runs, model_registry_events"))
    yield eng
    eng.dispose()


def _ok(engine: Engine, job: str, finished: datetime) -> None:
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO pipeline_runs (run_id, job, logical_time, started_at, finished_at,"
                " status) VALUES (gen_random_uuid(), :j, :t, :t, :t, 'ok')"
            ),
            {"j": job, "t": finished},
        )


def test_healthy_pipeline_passes_and_pings_success(
    engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    _ok(engine, "hourly", NOW - timedelta(hours=1))
    _ok(engine, "daily", NOW - timedelta(hours=20))
    pings: list[str] = []
    monkeypatch.setenv("HEALTHCHECKS_KEEPALIVE_URL", "https://hc.example/ping/k")
    out = keepalive.run(engine, now=lambda: NOW, ping=pings.append)
    assert out["status"] == "ok" and out["failed_checks"] == []
    assert pings == ["https://hc.example/ping/k"]


def test_stale_jobs_fail_and_the_loop_check_arms_after_the_first_retrain(
    engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    _ok(engine, "hourly", NOW - timedelta(hours=7))
    assert keepalive.checks(engine, NOW) == ["hourly_stale", "daily_stale"]
    _ok(engine, "hourly", NOW - timedelta(minutes=30))
    _ok(engine, "daily", NOW - timedelta(hours=10))
    assert keepalive.checks(engine, NOW) == []  # no retrain yet: loop check not armed
    runs.record(engine, "retrain", NOW - timedelta(days=9), "scheduled", "ok")
    assert keepalive.checks(engine, NOW) == ["loop_stale"]
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO model_registry_events (event_time, event_type, model_name,"
                " model_version, reason, triggered_by)"
                " VALUES (:t, 'rejected', 'lgbm', 'lgbm-x', 'test', 'schedule')"
            ),
            {"t": NOW - timedelta(days=2)},
        )
    assert keepalive.checks(engine, NOW) == []
    _ok(engine, "gate", NOW - timedelta(hours=49))  # the first gate run arms gate_stale
    assert keepalive.checks(engine, NOW) == ["gate_stale"]
    _ok(engine, "gate", NOW - timedelta(hours=23))
    assert keepalive.checks(engine, NOW) == []
    pings: list[str] = []
    monkeypatch.setenv("HEALTHCHECKS_KEEPALIVE_URL", "https://hc.example/ping/k")
    # three days later nothing new has run: hourly and daily are stale
    assert keepalive.run(engine, now=lambda: NOW + timedelta(days=3), ping=pings.append)[
        "failed_checks"
    ] == ["hourly_stale", "daily_stale", "gate_stale"]
    assert pings == ["https://hc.example/ping/k/fail"]
