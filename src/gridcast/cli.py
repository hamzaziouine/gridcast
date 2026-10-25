"""GridCast command-line interface (Typer)."""

import os
from datetime import UTC, datetime
from pathlib import Path

import typer
from sqlalchemy import Engine

from gridcast import __version__, db
from gridcast.timeutil import est_date

app = typer.Typer(
    name="gridcast",
    help="Ontario electricity demand forecaster: ingest, forecast, score, publish.",
    no_args_is_help=True,
)
db_app = typer.Typer(help="Database: migrations and connectivity.", no_args_is_help=True)
app.add_typer(db_app, name="db")
ingest_app = typer.Typer(help="Ingest live IESO files.", no_args_is_help=True)
app.add_typer(ingest_app, name="ingest")
prune_app = typer.Typer(help="Storage hygiene (retention rules).", no_args_is_help=True)
app.add_typer(prune_app, name="prune")
run_app = typer.Typer(help="Scheduled jobs (what the GitHub workflows run).", no_args_is_help=True)
app.add_typer(run_app, name="run")
drift_app = typer.Typer(help="Drift diagnostics.", no_args_is_help=True)
app.add_typer(drift_app, name="drift")
status_app = typer.Typer(help="Status-branch files (chart, metrics).", no_args_is_help=True)
app.add_typer(status_app, name="status")
gate_app = typer.Typer(help="Promotion gate: shadow -> champion, rollback.", no_args_is_help=True)
app.add_typer(gate_app, name="gate")


def _gate(action: str, apply: bool, run_kind: str, reason: str = "manual rollback") -> None:
    import json

    from gridcast.jobs import gate
    from gridcast.storage import objstore

    engine = _jobs_engine()
    try:
        out = gate.run(engine, objstore.private_store(), action, apply, reason, run_kind=run_kind)
    finally:
        engine.dispose()
    typer.echo(json.dumps(out, indent=1, default=str))
    if out["status"] == "failed":
        raise typer.Exit(code=1)


@gate_app.command("evaluate")
def gate_evaluate(
    apply: bool = typer.Option(False, "--apply", help="Write pointers/events. Default: dry run."),
    run_kind: str = typer.Option("manual", help="scheduled | manual"),
) -> None:
    """§4.7 steps 2-8: rollback check, shadow promote/hold/reject, queue hand-over."""
    _gate("evaluate", apply, run_kind)


@gate_app.command("rollback")
def gate_rollback(
    apply: bool = typer.Option(False, "--apply", help="Repoint champion.json. Default: dry run."),
    reason: str = typer.Option("manual rollback", help="Recorded in the registry event."),
) -> None:
    """Repoint champion.json to champion.prev.json; event rolled_back; 7-day cooldown."""
    _gate("rollback", apply, "manual", reason)


ISSUE_TIME_FORMATS = ["%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S"]


@app.callback()
def main() -> None:
    """GridCast CLI. Every scheduled job runs one of these commands."""


@app.command()
def version() -> None:
    """Print the GridCast version."""
    typer.echo(__version__)


@db_app.command("upgrade")
def db_upgrade(
    revision: str = typer.Argument("head", help="Alembic target revision."),
) -> None:
    """Apply Alembic migrations over DATABASE_URL_DIRECT (falls back to DATABASE_URL)."""
    try:
        url = db.migration_url()
    except db.DatabaseNotConfiguredError as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(code=2) from exc
    db.upgrade(url, revision)
    typer.echo(f"upgraded to {revision}")


@db_app.command("ping")
def db_ping(
    no_retry: bool = typer.Option(False, "--no-retry", help="Fail on the first error."),
) -> None:
    """Connect over DATABASE_URL (retrying for Neon cold starts) and print the server version."""
    try:
        url = db.jobs_url()
    except db.DatabaseNotConfiguredError as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(code=2) from exc
    engine = db.make_engine(url)
    try:
        version = db.ping(engine, waits=() if no_retry else db.DEFAULT_RETRY_WAITS_S)
    finally:
        engine.dispose()
    typer.echo(f"ok: {version}")


def _jobs_engine() -> Engine:
    try:
        return db.make_engine(db.jobs_url())
    except db.DatabaseNotConfiguredError as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(code=2) from exc


@ingest_app.command("realtime")
def ingest_realtime(
    lookback: int = typer.Option(6, help="Hours to look back for complete, unstored hours."),
) -> None:
    """RealtimeTotals: store complete hours of Market Demand (observed stamps)."""
    from gridcast.ingest import http, ieso_realtime

    engine = _jobs_engine()
    try:
        counts = ieso_realtime.ingest_recent(engine, http.fetch, datetime.now(UTC), lookback)
    finally:
        engine.dispose()
    typer.echo(" ".join(f"{k}={v}" for k, v in counts.items()))


@ingest_app.command("adequacy")
def ingest_adequacy(
    max_files: int = typer.Option(20, help="Cap on files downloaded this run (plan §6.1 step 0)."),
    days_ahead: int = typer.Option(2, help="Delivery dates today .. today + N (EST)."),
    archive_root: str = typer.Option(
        "data/raw", help="Local raw-XML archive root, used only when no object store is set."
    ),
) -> None:
    """Adequacy3: store every not-yet-read version for the next delivery dates, then prune."""
    from gridcast.ingest import http, ieso_adequacy
    from gridcast.storage import objstore

    store = objstore.private_store() if objstore.configured() else None
    engine = _jobs_engine()
    try:
        counts = ieso_adequacy.ingest_recent(
            engine,
            http.fetch,
            est_date(datetime.now(UTC)),
            days_ahead=days_ahead,
            max_files=max_files,
            archive_root=Path(archive_root),
            store=store,
        )
    finally:
        engine.dispose()
    typer.echo(" ".join(f"{k}={v}" for k, v in counts.items()))


@app.command()
def backtest(
    lgbm: bool = typer.Option(True, "--lgbm/--no-lgbm", help="Include LightGBM (slow)."),
    month: list[str] = typer.Option(  # noqa: B008 - Typer's documented pattern
        [], help="Fold month YYYY-MM (repeatable); default: the last 12 complete months."
    ),
    mlflow: bool = typer.Option(False, "--mlflow/--no-mlflow", help="Log the run to MLflow."),
    profile: str = typer.Option("full", help="LightGBM training profile: full | fast."),
) -> None:
    """Rolling-origin backtest (12 monthly folds) of the baselines and LightGBM; prints tables."""
    import time
    from datetime import date

    from gridcast.backtest.harness import Fold, load_actuals, run_backtest
    from gridcast.features.build import load_history, load_weather

    engine = _jobs_engine()
    try:
        raw, weather, actuals = load_history(engine), load_weather(engine), load_actuals(engine)
    finally:
        engine.dispose()
    folds = [
        Fold(k, date.fromisoformat(m + "-01")) for k, m in enumerate(sorted(month), start=1)
    ] or None
    t0 = time.monotonic()

    def log(msg: str) -> None:
        typer.echo(f"[{time.monotonic() - t0:7.1f}s] {msg}")

    res = run_backtest(raw, weather, actuals, folds=folds, with_lgbm=lgbm, log=log, profile=profile)
    typer.echo(res.lag_parity.to_string(index=False))
    typer.echo(res.summary().round(3).T.to_string())
    typer.echo(res.per_fold().round(2).to_string())
    if mlflow and lgbm:
        from gridcast.backtest.harness import headline
        from gridcast.registry import mlflow_client

        with mlflow_client.start_run("gridcast-backtest", f"backtest-{profile}"):
            mlflow_client.log({"folds": ",".join(f.month.isoformat() for f in res.folds),
                               "profile": profile}, headline(res), {})  # fmt: skip


@app.command()
def train(
    allow_fallback: bool = typer.Option(
        False,
        "--allow-fallback",
        help="If the hard floor fails, promote seasonal_naive_168 (only after 2026-10-30 12:00Z).",
    ),
    register_only: str = typer.Option(
        "", help="Skip training: register + bootstrap this already-uploaded model version."
    ),
    mlflow_run_id: str = typer.Option("", help="With --register-only: the MLflow run id."),
) -> None:
    """Backtest + final fit; artifact to the private bucket; bootstrap champion.json if absent."""
    import json

    from gridcast.jobs import train as train_job
    from gridcast.storage import objstore

    engine = _jobs_engine()
    try:
        store = objstore.private_store()
        if register_only:
            out = train_job.register(
                engine, store, register_only, allow_fallback, mlflow_run_id=mlflow_run_id or None
            )
        else:
            out = train_job.run(engine, store, allow_fallback, log=typer.echo)
    finally:
        engine.dispose()
    typer.echo(json.dumps(out, indent=1, default=str))


@app.command()
def predict(
    issue_time: datetime = typer.Option(  # noqa: B008 - Typer's documented pattern
        ...,
        "--issue-time",
        formats=["%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S"],
        help="Top of an hour; no offset means UTC (e.g. 2026-10-24T01:00:00+00:00).",
    ),
    weather: str = typer.Option(
        "store", help="'store' = weather_hourly; 'live' = fetch an Open-Meteo snapshot now."
    ),
    run_kind: str = typer.Option("manual", help="scheduled | catchup | manual"),
) -> None:
    """Write 24 x (champion + 3 baselines + ieso_adequacy) prediction rows for one issue time."""
    import json

    from gridcast.ingest import http, openmeteo
    from gridcast.jobs import predict as predict_job
    from gridcast.storage import objstore

    issue = issue_time if issue_time.tzinfo else issue_time.replace(tzinfo=UTC)
    engine = _jobs_engine()
    try:
        out = predict_job.run(
            engine,
            objstore.private_store(),
            issue,
            weather=weather,
            fetch_snapshot=lambda: openmeteo.fetch_live(lambda u, p: http.fetch(u, params=p)),
            run_kind=run_kind,
        )
    finally:
        engine.dispose()
    typer.echo(json.dumps(out, indent=1, default=str))


@run_app.command("hourly")
def run_hourly(
    issue_time: datetime | None = typer.Option(  # noqa: B008 - Typer's documented pattern
        None,
        "--issue-time",
        formats=ISSUE_TIME_FORMATS,
        help="Default: floor(now). No offset means UTC.",
    ),
    run_kind: str = typer.Option("scheduled", help="scheduled | catchup | manual"),
    catchup_hours: int = typer.Option(6, help="Missed issue hours to back-fill honestly."),
    publish: bool = typer.Option(True, "--publish/--no-publish", help="Write latest/*.json."),
) -> None:
    """Ingest realtime + adequacy, snapshot weather, forecast (with catch-up), publish."""
    import json

    from gridcast.ingest import http, openmeteo
    from gridcast.jobs import hourly
    from gridcast.storage import objstore

    issue = None
    if issue_time is not None:
        issue = issue_time if issue_time.tzinfo else issue_time.replace(tzinfo=UTC)
    deps = hourly.Deps(
        fetch=http.fetch,
        fetch_snapshot=lambda: openmeteo.fetch_live(lambda u, p: http.fetch(u, params=p)),
    )
    engine = _jobs_engine()
    try:
        out = hourly.run(
            engine,
            objstore.private_store(),
            objstore.public_store() if publish else None,
            deps,
            issue_time=issue,
            run_kind=run_kind,
            catchup_hours=catchup_hours,
        )
    finally:
        engine.dispose()
    typer.echo(json.dumps(out, indent=1, default=str))
    if out["status"] == "failed":
        raise typer.Exit(code=1)


@run_app.command("daily")
def run_daily(
    day: str = typer.Option("", "--date", help="EST date YYYY-MM-DD; default today (EST)."),
    run_kind: str = typer.Option("scheduled", help="scheduled | catchup | manual"),
    publish: bool = typer.Option(True, "--publish/--no-publish", help="Write scoreboard.json."),
) -> None:
    """PUB_Demand ingest, scoring, peak hours, score_daily, drift signals, scoreboard.json."""
    import json
    from datetime import date

    from gridcast.ingest import http
    from gridcast.jobs import daily
    from gridcast.storage import objstore

    engine = _jobs_engine()
    try:
        out = daily.run(
            engine,
            objstore.private_store(),
            objstore.public_store() if publish else None,
            daily.Deps(fetch=http.fetch),
            day=date.fromisoformat(day) if day else None,
            run_kind=run_kind,
        )
    finally:
        engine.dispose()
    typer.echo(json.dumps(out, indent=1, default=str))
    gh_output = os.environ.get("GITHUB_OUTPUT")
    if gh_output:  # daily.yml dispatches retrain.yml on a drift trigger
        with open(gh_output, "a", encoding="utf-8") as fh:
            fh.write(f"retrain_reason={out.get('retrain_trigger') or ''}\n")
    if out["status"] == "failed":
        raise typer.Exit(code=1)


@run_app.command("retrain")
def run_retrain(
    reason: str = typer.Option("manual", help="weekly | manual | drift:<rule>"),
    profile: str = typer.Option("fast", help="LightGBM training profile: fast | full."),
    run_kind: str = typer.Option("manual", help="scheduled | manual"),
) -> None:
    """Train a candidate, backtest it on the champion's folds, register shadow/queue/reject."""
    import json
    import time

    import httpx

    from gridcast.ingest import http
    from gridcast.jobs import retrain
    from gridcast.storage import objstore

    t0 = time.monotonic()

    def log(msg: str) -> None:
        typer.echo(f"[{time.monotonic() - t0:7.1f}s] {msg}")

    engine = _jobs_engine()
    try:
        with httpx.Client(
            timeout=http.TIMEOUT_S, headers={"User-Agent": http.USER_AGENT}
        ) as client:
            deps = retrain.Deps(
                fetch_weather=lambda u, p: http.fetch(u, params=p, client=client, attempts=6)
            )
            out = retrain.run(
                engine, objstore.private_store(), deps, reason, profile, run_kind, log=log
            )
    finally:
        engine.dispose()
    out["runtime_s"] = round(time.monotonic() - t0, 1)
    typer.echo(json.dumps(out, indent=1, default=str))
    if out["status"] == "failed":
        raise typer.Exit(code=1)


@run_app.command("keepalive")
def run_keepalive(
    run_kind: str = typer.Option("scheduled", help="scheduled | manual"),
) -> None:
    """Backstop checks after keepalive.yml re-enabled the scheduled workflows (plan §6.7)."""
    import json

    from gridcast.jobs import keepalive

    engine = _jobs_engine()
    try:
        out = keepalive.run(engine, run_kind=run_kind)
    finally:
        engine.dispose()
    typer.echo(json.dumps(out, indent=1, default=str))
    if out["status"] == "failed":
        raise typer.Exit(code=1)


@run_app.command("prune")
def run_prune(
    apply: bool = typer.Option(False, "--apply", help="Delete. Default: dry run (counts only)."),
    run_kind: str = typer.Option("manual", help="scheduled | manual"),
) -> None:
    """Retention (plan §6.8): weak names > 90 d with their scores, realtime versions > 60 d."""
    import json

    from gridcast.jobs import prune

    def direct() -> Engine:
        return db.make_engine(db.migration_url(), isolation_level="AUTOCOMMIT")

    engine = _jobs_engine()
    try:
        out = prune.run(engine, direct, apply_changes=apply, run_kind=run_kind)
    finally:
        engine.dispose()
    typer.echo(json.dumps(out, indent=1, default=str))
    if out["status"] == "failed":
        raise typer.Exit(code=1)


@drift_app.command("reference")
def drift_reference(
    version: str = typer.Option("", help="Model version; default: the current champion."),
) -> None:
    """Build models/<version>/reference_features.parquet (PSI reference) from the store data."""
    from gridcast.backtest.harness import load_actuals
    from gridcast.drift import psi
    from gridcast.features.build import load_history, load_weather
    from gridcast.jobs.predict import CHAMPION_KEY
    from gridcast.storage import objstore

    store = objstore.private_store()
    version = version or str(store.get_json(CHAMPION_KEY)["model_version"])
    engine = _jobs_engine()
    try:
        raw, weather, actuals = load_history(engine), load_weather(engine), load_actuals(engine)
    finally:
        engine.dispose()
    ref = psi.build_reference(raw, weather, actuals)
    blob = psi.to_parquet(ref)
    store.put_bytes(psi.reference_key(version), blob, "application/vnd.apache.parquet")
    typer.echo(f"{psi.reference_key(version)}: {len(ref)} rows, {len(blob)} bytes")


@status_app.command("build")
def status_build(
    out: Path = typer.Option(Path("status_out"), help="Directory to write the files into."),  # noqa: B008
) -> None:
    """chart_7d.svg + metrics.json + a mirror of latest/*.json, for the orphan `status` branch."""
    import json

    from gridcast.status import build
    from gridcast.storage import objstore

    configured = objstore.configured()
    engine = _jobs_engine()
    try:
        res = build.build(
            engine,
            objstore.private_store() if configured else None,
            objstore.public_store() if configured else None,
            out,
            datetime.now(UTC),
        )
    finally:
        engine.dispose()
    typer.echo(json.dumps(res, indent=1, default=str))


@prune_app.command("adequacy")
def prune_adequacy(
    vacuum_full: bool = typer.Option(
        False, "--vacuum-full", help="Then VACUUM FULL the table over DATABASE_URL_DIRECT."
    ),
) -> None:
    """Adequacy3 retention: keep versions created >= D-2 00:00 EST plus the newest older one."""
    from sqlalchemy import text

    from gridcast.ingest import ieso_adequacy

    engine = _jobs_engine()
    try:
        out = ieso_adequacy.prune_snapshots(engine)
    finally:
        engine.dispose()
    typer.echo(" ".join(f"{k}={v}" for k, v in out.items()))
    if vacuum_full:
        direct = db.make_engine(db.migration_url(), isolation_level="AUTOCOMMIT")
        try:
            with direct.connect() as conn:
                conn.execute(text("VACUUM FULL ANALYZE ieso_forecast_snapshots"))
                size = conn.execute(
                    text("SELECT pg_total_relation_size('ieso_forecast_snapshots')")
                ).scalar_one()
        finally:
            direct.dispose()
        typer.echo(f"vacuum full done; ieso_forecast_snapshots = {size} bytes")


if __name__ == "__main__":
    app()
