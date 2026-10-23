"""GridCast command-line interface (Typer)."""

import typer

from gridcast import __version__

app = typer.Typer(
    name="gridcast",
    help="Ontario electricity demand forecaster: ingest, forecast, score, publish.",
    no_args_is_help=True,
)


@app.callback()
def main() -> None:
    """GridCast CLI. Every scheduled job runs one of these commands."""


@app.command()
def version() -> None:
    """Print the GridCast version."""
    typer.echo(__version__)


if __name__ == "__main__":
    app()
