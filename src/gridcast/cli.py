"""GridCast command-line interface (Typer)."""

import typer

from gridcast import __version__, db

app = typer.Typer(
    name="gridcast",
    help="Ontario electricity demand forecaster: ingest, forecast, score, publish.",
    no_args_is_help=True,
)
db_app = typer.Typer(help="Database: migrations and connectivity.", no_args_is_help=True)
app.add_typer(db_app, name="db")


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


if __name__ == "__main__":
    app()
