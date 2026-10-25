"""`gridcast run keepalive` (C16, BUILD_PLAN §6.7): the checks after keepalive.yml re-enables
every scheduled workflow.

keepalive.yml step 1 (`gh api -X PUT .../actions/workflows/<file>/enable` for every workflow with
a `schedule:` trigger, itself included) is what beats GitHub's 60-day auto-disable. This command is
the second line, run right after it:

- `hourly_stale`: no `ok` hourly run finished in the last 6 h (Healthchecks catches this sooner;
  this is the backstop if the heartbeat secret is missing).
- `daily_stale`: no `ok` daily run finished in the last 48 h.
- `loop_stale` (plan §6.7 step 3, armed only once the loop has run at least once, i.e. a
  `retrain` pipeline run exists): no registry event of the loop kinds in the last 8 days.
- `gate_stale` (plan §6.7 step 3, armed once a `gate` pipeline run exists; dry runs write none):
  no `ok` gate run in the last 48 h.

Plan §6.7 step 2 (status-branch commit < 48 h) is not built: there is no status branch; the
public record is the R2 bucket (ADR-0011), whose freshness `hourly_stale` covers.
Any failed check -> `failed` + HEALTHCHECKS_KEEPALIVE_URL/fail; else `ok` + success ping.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import Engine, text

from gridcast.config import get_settings
from gridcast.jobs import runs
from gridcast.timeutil import floor_hour

JOB = "keepalive"
HOURLY_MAX_AGE = timedelta(hours=6)
DAILY_MAX_AGE = timedelta(hours=48)
LOOP_MAX_AGE = timedelta(days=8)
GATE_MAX_AGE = timedelta(hours=48)  # plan §6.7 step 3
LOOP_EVENTS = (
    "trained",
    "backtested",
    "rejected",
    "shadow_started",
    "promoted",
    "candidate_queued",
)


def _newest_ok(engine: Engine, job: str) -> datetime | None:
    with engine.connect() as conn:
        v = conn.execute(
            text("SELECT max(finished_at) FROM pipeline_runs WHERE job = :j AND status = 'ok'"),
            {"j": job},
        ).scalar()
    return None if v is None else v.astimezone(UTC)


def checks(engine: Engine, now: datetime) -> list[str]:
    """Names of the failed checks (empty = healthy)."""
    failed = []
    hourly = _newest_ok(engine, "hourly")
    if hourly is None or now - hourly > HOURLY_MAX_AGE:
        failed.append("hourly_stale")
    daily = _newest_ok(engine, "daily")
    if daily is None or now - daily > DAILY_MAX_AGE:
        failed.append("daily_stale")
    with engine.connect() as conn:
        loop_armed = conn.execute(
            text("SELECT 1 FROM pipeline_runs WHERE job = 'retrain' LIMIT 1")
        ).first()
        newest_event = conn.execute(
            text("SELECT max(event_time) FROM model_registry_events WHERE event_type = ANY(:e)"),
            {"e": list(LOOP_EVENTS)},
        ).scalar()
    if loop_armed is not None and (
        newest_event is None or now - newest_event.astimezone(UTC) > LOOP_MAX_AGE
    ):
        failed.append("loop_stale")
    with engine.connect() as conn:  # armed once gate.yml has applied at least once
        gate_armed = conn.execute(
            text("SELECT 1 FROM pipeline_runs WHERE job = 'gate' LIMIT 1")
        ).first()
    gate = _newest_ok(engine, "gate")
    if gate_armed is not None and (gate is None or now - gate > GATE_MAX_AGE):
        failed.append("gate_stale")
    return failed


def run(
    engine: Engine,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
    ping: runs.Pinger = runs.http_get,
    run_kind: str = "scheduled",
) -> dict[str, Any]:
    t = now()
    run_id = runs.start(engine, JOB, floor_hour(t), run_kind)
    failed = checks(engine, t)
    status = runs.finish(engine, run_id, "failed" if failed else "ok", 0, failed)
    hc = get_settings().healthchecks_keepalive_url
    return {
        "status": status,
        "failed_checks": failed,
        "heartbeat": runs.heartbeat(hc, status != "failed", ping),
    }
