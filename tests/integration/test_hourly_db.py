"""C10 against real Postgres + moto: `gridcast run hourly` dedupe, catch-up, degrade, publish."""

from __future__ import annotations

import json
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from moto import mock_aws
from sqlalchemy import Engine, text

from gridcast import db
from gridcast.jobs import hourly, publish
from gridcast.models import lgbm as lgbm_mod
from gridcast.storage.objstore import ObjectStore
from tests.integration.helpers import (
    Clock,
    champion_count,
    ieso_fetch,
    insert_ieso_snapshot,
    install_champion,
    make_buckets,
    snapshot_fetcher,
)
from tests.integration.test_features_db import END, START, load_synthetic

pytestmark = pytest.mark.integration

T0 = datetime(2024, 1, 25, 3, tzinfo=UTC)


@pytest.fixture(scope="module")
def env(fresh_db_url: str) -> Iterator[tuple[Engine, ObjectStore, ObjectStore]]:
    db.upgrade(fresh_db_url)
    eng = db.make_engine(fresh_db_url)
    load_synthetic(eng)
    insert_ieso_snapshot(eng, T0 - timedelta(hours=27), T0 - timedelta(hours=24), 96)
    with pytest.MonkeyPatch.context() as mp, mock_aws():
        mp.setattr(lgbm_mod, "MAX_ROUNDS", 5)
        mp.delenv("HEALTHCHECKS_HOURLY_URL", raising=False)
        private, public = make_buckets()
        install_champion(eng, private, START, END)
        yield eng, private, public
    eng.dispose()


def _run(env: tuple[Engine, ObjectStore, ObjectStore], now: datetime, **kw: Any) -> dict[str, Any]:
    eng, private, public = env
    clock = Clock(now)
    deps = hourly.Deps(
        fetch=ieso_fetch(),
        fetch_snapshot=kw.pop("fetch_snapshot", snapshot_fetcher(clock)),
        now=clock,
    )
    return hourly.run(eng, private, public, deps, **kw)


def _runs(engine: Engine, issue: datetime) -> list[tuple[str, str]]:
    with engine.connect() as conn:
        return [
            (r[0], r[1])
            for r in conn.execute(
                text(
                    "SELECT run_kind, status FROM pipeline_runs WHERE job = 'hourly'"
                    " AND logical_time = :t ORDER BY started_at"
                ),
                {"t": issue},
            )
        ]


def test_first_run_forecasts_and_leaves_honest_gaps(env: Any) -> None:
    eng = env[0]
    out = _run(env, T0 + timedelta(minutes=7))
    assert out["status"] == "ok", out
    assert out["rows_written"]["champion"] == 24
    assert out["realtime"]["missing"] == 6  # every RealtimeTotals hour 404: degraded, not failed
    assert out["weather"] == "stored"
    assert champion_count(eng, T0) == 24
    # 6 catch-up hours: no snapshot existed at their cutoffs -> baselines + IESO only, no champion
    assert len(out["catchup"]) == 6
    for h in range(1, 7):
        assert champion_count(eng, T0 - timedelta(hours=h)) == 0
        assert _runs(eng, T0 - timedelta(hours=h)) == [("catchup", "ok")]
    assert any("no qualifying weather snapshot" in n for n in out["notes"])
    assert any(n.startswith("adequacy_stale") for n in out["notes"])
    with eng.connect() as conn:
        idx = conn.execute(text("SELECT issue_time, n_rows FROM weather_snapshot_index")).all()
    assert [(r[0].astimezone(UTC), r[1]) for r in idx] == [(T0, 72)]


def test_rerun_is_skipped_and_writes_nothing(env: Any) -> None:
    eng = env[0]
    out = _run(env, T0 + timedelta(minutes=27))
    assert out["status"] == "skipped"
    assert _runs(eng, T0) == [("scheduled", "ok"), ("scheduled", "skipped")]
    with eng.connect() as conn:
        n = conn.execute(
            text("SELECT count(*) FROM weather_snapshot_index")
        ).scalar_one()  # no second fetch
    assert n == 1


def test_catchup_uses_only_snapshots_from_before_each_cutoff(env: Any) -> None:
    eng, _, public = env
    issue = T0 + timedelta(hours=3)  # 04:00 and 05:00 were missed
    out = _run(env, issue + timedelta(minutes=7))
    assert out["status"] == "ok", out
    assert set(out["catchup"]) == {
        (T0 + timedelta(hours=1)).isoformat(),
        (T0 + timedelta(hours=2)).isoformat(),
    }
    for h in (1, 2):
        missed = T0 + timedelta(hours=h)
        assert champion_count(eng, missed) == 24  # from the 03:07 snapshot (<= its cutoff)
        with eng.connect() as conn:
            kinds = conn.execute(
                text(
                    "SELECT DISTINCT run_kind FROM v_predictions WHERE issue_time = :i"
                    " AND model_name = 'champion'"
                ),
                {"i": missed},
            ).all()
        assert kinds == [("catchup",)]
    fc = json.loads(public.get_bytes(publish.FORECAST_KEY))
    assert fc["issue_time"] == "2024-01-25T06:00:00Z"
    assert len(fc["rows"]) == 24 and fc["rows"][0]["horizon"] == 1
    assert set(fc["rows"][0]) == {
        "horizon", "target_time", "p10", "p50", "p90", "ieso_adequacy", "ieso_staleness_min",
        "seasonal_naive_168", "seasonal_naive_24", "persistence",
    }  # fmt: skip
    assert fc["rows"][0]["ieso_adequacy"] is not None
    meta = json.loads(public.get_bytes(publish.META_KEY))
    assert meta["last_issue_time"] == "2024-01-25T06:00:00Z"
    assert meta["champion"]["model_version"] == "lgbm-test"
    assert meta["last_run"]["status"] == "ok"


def test_weather_outage_falls_back_to_an_earlier_snapshot(env: Any) -> None:
    eng = env[0]
    issue = T0 + timedelta(hours=4)

    def broken() -> Any:
        raise RuntimeError("live forecast: HTTP 503")

    out = _run(env, issue + timedelta(minutes=7), fetch_snapshot=broken, catchup_hours=0)
    assert out["status"] == "ok", out  # 06:07 snapshot still covers 07:00 and is <= its cutoff
    assert champion_count(eng, issue) == 24
    assert "weather: RuntimeError" in out["notes"]


def test_no_qualifying_weather_fails_the_hour_but_still_writes_baselines(env: Any) -> None:
    eng, private, public = env
    issue = T0 + timedelta(days=3)  # every stored snapshot is too old to cover its targets
    pings: list[str] = []

    def broken() -> Any:
        raise RuntimeError("down")

    deps = hourly.Deps(
        fetch=ieso_fetch(), fetch_snapshot=broken, now=Clock(issue), ping=pings.append
    )
    with pytest.MonkeyPatch.context() as mp:
        mp.setenv("HEALTHCHECKS_HOURLY_URL", "https://hc.example/ping/abc")
        out = hourly.run(eng, private, public, deps, catchup_hours=0)
    assert out["status"] == "failed"
    assert champion_count(eng, issue) == 0
    assert out["rows_written"]["seasonal_naive_168"] == 24
    assert pings == ["https://hc.example/ping/abc/fail"]
    assert _runs(eng, issue) == [("scheduled", "failed")]


def test_late_run_after_the_cutoff_uses_the_previous_snapshot_and_pings_success(
    env: Any,
) -> None:
    """GitHub cron lateness (ADR-0012): a run that starts after issue + 30 min cannot use its own
    snapshot, but the previous slot's snapshot (fetched before the cutoff) still covers the hour,
    so the champion forecast is honest and the heartbeat is a success, not /fail."""
    eng, private, public = env
    first = T0 + timedelta(days=5)  # far from the other tests' snapshots
    pings: list[str] = []

    def run_at(now: datetime) -> dict[str, Any]:
        clock = Clock(now)
        deps = hourly.Deps(
            fetch=ieso_fetch(), fetch_snapshot=snapshot_fetcher(clock), now=clock, ping=pings.append
        )
        return hourly.run(eng, private, public, deps, catchup_hours=0)

    with pytest.MonkeyPatch.context() as mp:
        mp.setenv("HEALTHCHECKS_HOURLY_URL", "https://hc.example/ping/abc")
        assert run_at(first + timedelta(minutes=17))["status"] == "ok"
        late = first + timedelta(hours=1)
        out = run_at(late + timedelta(minutes=48))  # 18 min after data_cutoff
        assert out["status"] == "ok", out
        assert out["weather"] == "stored"  # fetched for the next hour, unusable for this one
        assert champion_count(eng, late) == 24
        # a duplicate slot for the same issue hour is skipped and still pings success
        assert run_at(late + timedelta(minutes=55))["status"] == "skipped"
    assert pings == ["https://hc.example/ping/abc"] * 3
    with eng.connect() as conn:
        slots = conn.execute(
            text(
                "SELECT issue_time, available_at FROM weather_snapshot_index"
                " WHERE issue_time IN (:a, :b) ORDER BY 1"
            ),
            {"a": first, "b": late},
        ).all()
    # the late slot's own snapshot is after its cutoff; only the previous slot's qualified
    assert [r[1].astimezone(UTC) for r in slots] == [
        first + timedelta(minutes=17),
        late + timedelta(minutes=48),
    ]
    assert _runs(eng, late) == [("scheduled", "ok"), ("scheduled", "skipped")]
