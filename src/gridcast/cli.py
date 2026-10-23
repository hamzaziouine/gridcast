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
    archive_root: str = typer.Option("data/raw", help="Local raw-XML archive root."),
) -> None:
    """Adequacy3: store every not-yet-stored version for the next delivery dates."""
    from gridcast.ingest import http, ieso_adequacy

    engine = _jobs_engine()
    try:
        counts = ieso_adequacy.ingest_recent(
            engine,
            http.fetch,
            est_date(datetime.now(UTC)),
            days_ahead=days_ahead,
            max_files=max_files,
            archive_root=Path(archive_root),
        )
    finally:
        engine.dispose()
    typer.echo(" ".join(f"{k}={v}" for k, v in counts.items()))


@app.command()
def backtest(
    lgbm: bool = typer.Option(False, "--lgbm/--no-lgbm", help="Include LightGBM (C8)."),
    month: list[str] = typer.Option(  # noqa: B008 - Typer's documented pattern
        [], help="Fold month YYYY-MM (repeatable); default: the last 12 complete months."
    ),
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


if __name__ == "__main__":
    app()
