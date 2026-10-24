"""`gridcast run hourly` (C10, BUILD_PLAN §6.1 as amended by §3.0 and DATA_NOTES §17).

Steps, for `issue_time = floor(now)` (or `--issue-time`), `data_cutoff = issue_time + 30 min`:

0. Adequacy3: every not-yet-read version for today .. today+2 (EST), archived to the private
   bucket, retention prune; its own `pipeline_runs` row (job `ieso_adequacy`, logical_time = the
   run's minute). Runs before the dedupe check, so the second cron slot picks up the ~:25 reissue.
1. Dedupe: an `ok` row for (hourly, issue_time) -> this run is `skipped`, exit 0.
2. RealtimeTotals: complete hours of the last 6 (404 / partial hours are skipped, never fatal).
3. Live Open-Meteo snapshot -> private bucket Parquet + `weather_snapshot_index`
   (available_at = fetched_at), unless this slot already has one.
4. Catch-up: issue hours in the last `catchup_hours` with no champion row and no `ok` hourly run,
   oldest first, `run_kind='catchup'`. Then the issue hour itself. Each hour uses its own cutoff:
   demand and IESO as of that cutoff, weather only from the newest snapshot with
   available_at <= cutoff that covers the hour (`weather_snapshots.qualifying`). No qualifying
   snapshot -> no champion row for that hour (a visible gap), baselines + IESO still written.
5. Freshness: newest Adequacy3 snapshot older than 6 h -> note `adequacy_stale` (degraded, not a
   failure: IESO outages never fail the job, §3.0 item 6).
6. Publish `latest/forecast.json` + `latest/meta.json` to the public bucket.
7. Close the run row; heartbeat (HEALTHCHECKS_HOURLY_URL, optional).

Status: `ok` when the issue hour has 24 champion rows; `failed` when it does not (no weather,
model or database error) or publishing failed. Every failure is recorded in `notes` and the job
still writes everything it can. Scoring is daily only (§3.0 decision 1: no provisional actuals).
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import Engine, text

from gridcast.config import get_settings
from gridcast.ingest import ieso_adequacy, ieso_realtime, weather_snapshots
from gridcast.ingest.http import Fetched
from gridcast.jobs import predict, publish, runs
from gridcast.storage.objstore import ObjectStore
from gridcast.timeutil import HOUR, est_date, floor_hour

JOB = "hourly"
CATCHUP_HOURS = 6
REALTIME_LOOKBACK_H = 6
ADEQUACY_MAX_AGE = timedelta(hours=6)


@dataclass
class Deps:
    """Everything that talks to the outside world, injectable for tests."""

    fetch: Callable[[str], Fetched]  # IESO reports-public
    fetch_snapshot: weather_snapshots.FetchSnapshot  # Open-Meteo live forecast
    now: Callable[[], datetime] = field(default=lambda: datetime.now(UTC))
    ping: runs.Pinger = runs.http_get


def catchup_candidates(engine: Engine, issue: datetime, hours: int) -> list[datetime]:
    if hours <= 0:
        return []
    with engine.connect() as conn:
        rows = conn.execute(
            text(
                "SELECT h FROM generate_series(CAST(:lo AS timestamptz), CAST(:hi AS timestamptz),"
                " interval '1 hour') AS h"
                " WHERE NOT EXISTS (SELECT 1 FROM prediction_runs r"
                "                   WHERE r.issue_time = h AND r.model_id = 1)"
                " AND NOT EXISTS (SELECT 1 FROM pipeline_runs p WHERE p.job = :job"
                "                 AND p.logical_time = h AND p.status = 'ok')"
                " ORDER BY h"
            ),
            {"lo": issue - hours * HOUR, "hi": issue - HOUR, "job": JOB},
        ).all()
    return [r[0].astimezone(UTC) for r in rows]


def _adequacy(engine: Engine, store: ObjectStore, deps: Deps, now: datetime) -> dict[str, Any]:
    slot = now.replace(second=0, microsecond=0)
    try:
        counts = ieso_adequacy.ingest_recent(engine, deps.fetch, est_date(now), store=store)
    except Exception as exc:
        n = runs.Notes()
        n.error("adequacy", exc)
        runs.record(engine, "ieso_adequacy", slot, "scheduled", "failed", 0, n.private)
        return {"status": "failed", "notes": n}
    status = runs.record(engine, "ieso_adequacy", slot, "scheduled", "ok", counts["rows"])
    return {"status": status, **counts}


def _predict_hour(
    engine: Engine, store: ObjectStore, issue: datetime, run_kind: str, notes: runs.Notes
) -> dict[str, int]:
    wx = weather_snapshots.qualifying(engine, store, issue)
    if wx is None:
        notes.add(f"no qualifying weather snapshot for {issue:%Y-%m-%dT%HZ}: champion skipped")
    out = predict.run(
        engine,
        store,
        issue,
        weather="frame" if wx is not None else "none",
        weather_frame=wx,
        run_kind=run_kind,
    )
    return dict(out["rows_written"])


def _champion_rows(engine: Engine, issue: datetime) -> int:
    with engine.connect() as conn:
        return int(
            conn.execute(
                text("SELECT count(*) FROM predictions WHERE issue_time = :i AND model_id = 1"),
                {"i": issue},
            ).scalar_one()
        )


def run(
    engine: Engine,
    store: ObjectStore,
    public: ObjectStore | None,
    deps: Deps,
    issue_time: datetime | None = None,
    run_kind: str = "scheduled",
    catchup_hours: int = CATCHUP_HOURS,
) -> dict[str, Any]:
    now = deps.now()
    issue = floor_hour(issue_time or now)
    hc_url = get_settings().healthchecks_hourly_url
    out: dict[str, Any] = {"issue_time": issue.isoformat(), "run_kind": run_kind}
    adequacy = _adequacy(engine, store, deps, now)

    run_id = runs.start(engine, JOB, issue, run_kind)
    if runs.already_ok(engine, JOB, issue):
        out["status"] = runs.finish(engine, run_id, "skipped", 0, ["already ok"])
        out["heartbeat"] = runs.heartbeat(hc_url, True, deps.ping)
        return out

    notes = runs.Notes()
    if isinstance(adequacy.get("notes"), runs.Notes):
        failed: runs.Notes = adequacy.pop("notes")
        notes.private += failed.private
        notes.public += failed.public
    out["adequacy"] = adequacy
    try:
        out["realtime"] = ieso_realtime.ingest_recent(
            engine, deps.fetch, now, lookback_h=REALTIME_LOOKBACK_H
        )
    except Exception as exc:
        notes.error("realtime", exc)
    try:
        out["weather"] = weather_snapshots.fetch_and_store(engine, store, deps.fetch_snapshot, now)
    except Exception as exc:
        notes.error("weather", exc)

    written = 0
    out["catchup"] = {}
    for h in catchup_candidates(engine, issue, catchup_hours):
        hour = runs.Notes()
        try:
            res = _predict_hour(engine, store, h, "catchup", hour)
            runs.record(engine, JOB, h, "catchup", "ok", sum(res.values()), hour.private)
            written += sum(res.values())
            out["catchup"][h.isoformat()] = res
        except Exception as exc:
            hour.error(f"catchup {h:%Y-%m-%dT%HZ}", exc)
            runs.record(engine, JOB, h, "catchup", "failed", 0, hour.private)
        notes.private += hour.private
        notes.public += hour.public

    try:
        res = _predict_hour(engine, store, issue, run_kind, notes)
        written += sum(res.values())
        out["rows_written"] = res
    except Exception as exc:
        notes.error("predict", exc)

    champion_rows = _champion_rows(engine, issue)
    status = "ok" if champion_rows == 24 else "failed"
    if status == "failed":
        notes.add(f"champion rows for issue_time: {champion_rows}/24")

    with engine.connect() as conn:
        newest = conn.execute(text("SELECT max(created_at) FROM ieso_forecast_snapshots")).scalar()
    if newest is None or now - newest > ADEQUACY_MAX_AGE:
        notes.add("adequacy_stale: newest Adequacy3 snapshot older than 6 h")

    champion: dict[str, Any] | None = None
    try:
        champion = store.get_json(predict.CHAMPION_KEY)
    except Exception as exc:
        notes.error("champion.json", exc)
    if public is not None:
        run_info = {
            "job": JOB,
            "logical_time": publish.iso(issue),
            "status": status,
            "run_kind": run_kind,
            "notes": list(notes.public),
        }
        try:
            out["published"] = publish.publish_hourly(engine, public, champion, run_info, now)
        except Exception as exc:
            notes.error("publish", exc)
            status = "failed"

    out["status"] = runs.finish(engine, run_id, status, written, notes.private)
    out["rows_written_total"] = written
    out["notes"] = notes.public
    out["heartbeat"] = runs.heartbeat(hc_url, out["status"] != "failed", deps.ping)
    return out
