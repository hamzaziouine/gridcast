"""GridCast command-line interface (Typer)."""

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
) -> None:
    """Rolling-origin backtest (12 monthly folds) of the baselines and LightGBM; prints tables."""
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
    res = run_backtest(raw, weather, actuals, folds=folds, with_lgbm=lgbm, log=typer.echo)
    typer.echo(res.lag_parity.to_string(index=False))
    typer.echo(res.summary().round(3).T.to_string())
    typer.echo(res.per_fold().round(2).to_string())
    if mlflow and lgbm:
        from gridcast.backtest.harness import headline
        from gridcast.registry import mlflow_client

        with mlflow_client.start_run("gridcast-backtest", "backtest"):
            mlflow_client.log({"folds": ",".join(f.month.isoformat() for f in res.folds)},
                              headline(res), {})  # fmt: skip


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
