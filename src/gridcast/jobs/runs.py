"""`pipeline_runs` bookkeeping and the Healthchecks.io heartbeat (BUILD_PLAN §6 preamble).

- Every job writes a `running` row at start and `ok|skipped|failed` at the end.
- Dedupe: only one `ok` per (job, logical_time) (partial unique index). A run that finds an `ok`
  already there marks itself `skipped`; a unique violation when writing `ok` is a lost race and
  also ends as `skipped`, never `failed`.
- Heartbeat: GET <url> on success, <url>/fail on failure; a no-op when the URL is unset. The URL is
  never printed (it is a credential of sorts).
"""

from __future__ import annotations

import os
import uuid
from collections.abc import Callable, Sequence
from datetime import datetime

import httpx
from sqlalchemy import Engine, text
from sqlalchemy.exc import IntegrityError

Pinger = Callable[[str], object]


def start(engine: Engine, job: str, logical_time: datetime, run_kind: str) -> uuid.UUID:
    run_id = uuid.uuid4()
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO pipeline_runs (run_id, job, logical_time, run_kind, started_at,"
                " status, github_run_id) VALUES (:id, :job, :t, :k, now(), 'running', :gh)"
            ),
            {
                "id": run_id,
                "job": job,
                "t": logical_time,
                "k": run_kind,
                "gh": os.environ.get("GITHUB_RUN_ID"),
            },
        )
    return run_id


def already_ok(engine: Engine, job: str, logical_time: datetime) -> bool:
    with engine.connect() as conn:
        return (
            conn.execute(
                text(
                    "SELECT 1 FROM pipeline_runs WHERE job = :job AND logical_time = :t"
                    " AND status = 'ok'"
                ),
                {"job": job, "t": logical_time},
            ).first()
            is not None
        )


def finish(
    engine: Engine,
    run_id: uuid.UUID,
    status: str,
    rows_written: int = 0,
    notes: Sequence[str] = (),
) -> str:
    """Close the run; returns the status actually stored (`ok` may become `skipped`)."""
    sql = text(
        "UPDATE pipeline_runs SET status = :s, finished_at = now(), rows_written = :n,"
        " notes = :notes WHERE run_id = :id"
    )
    params = {"id": run_id, "s": status, "n": rows_written, "notes": "; ".join(notes) or None}
    try:
        with engine.begin() as conn:
            conn.execute(sql, params)
    except IntegrityError:  # another run wrote `ok` for this logical_time first
        params |= {"s": "skipped", "notes": "; ".join([*notes, "lost ok race"])}
        with engine.begin() as conn:
            conn.execute(sql, params)
        return "skipped"
    return status


def record(
    engine: Engine,
    job: str,
    logical_time: datetime,
    run_kind: str,
    status: str,
    rows_written: int = 0,
    notes: Sequence[str] = (),
) -> str:
    """A finished run in one call (sub-steps and catch-up hours)."""
    return finish(engine, start(engine, job, logical_time, run_kind), status, rows_written, notes)


def http_get(url: str) -> object:
    return httpx.get(url, timeout=10.0)


def heartbeat(url: str | None, ok: bool, ping: Pinger = http_get) -> str:
    """Ping Healthchecks.io; returns 'sent', 'disabled' or 'error' (never raises)."""
    if not url:
        return "disabled"
    try:
        ping(url.rstrip("/") + ("" if ok else "/fail"))
    except Exception:  # a monitoring outage must not fail the job
        return "error"
    return "sent"
