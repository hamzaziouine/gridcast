"""Database access: engine factory, URL normalisation, retrying ping, Alembic entry point."""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path
from typing import Any

from sqlalchemy import Engine, create_engine, text
from tenacity import Retrying, stop_after_attempt, wait_chain, wait_fixed

from gridcast.config import get_settings

# Neon cold starts: retry after 5, 15 and 30 s (BUILD_PLAN §5, db.py).
DEFAULT_RETRY_WAITS_S: tuple[float, ...] = (5, 15, 30)

ALEMBIC_INI = Path(__file__).resolve().parents[2] / "alembic.ini"


class DatabaseNotConfiguredError(RuntimeError):
    """Raised when no database URL is set."""


def normalize_url(url: str) -> str:
    """Force the psycopg 3 driver for plain `postgres://` / `postgresql://` URLs (Neon's form)."""
    for prefix in ("postgresql+psycopg://", "postgresql://", "postgres://"):
        if url.startswith(prefix):
            return "postgresql+psycopg://" + url[len(prefix) :]
    return url


def jobs_url() -> str:
    """Pooled URL used by every job (DATABASE_URL)."""
    url = get_settings().database_url
    if not url:
        raise DatabaseNotConfiguredError("DATABASE_URL is not set")
    return url


def migration_url() -> str:
    """Non-pooled URL for Alembic (DATABASE_URL_DIRECT, falling back to DATABASE_URL)."""
    settings = get_settings()
    url = settings.database_url_direct or settings.database_url
    if not url:
        raise DatabaseNotConfiguredError("neither DATABASE_URL_DIRECT nor DATABASE_URL is set")
    return url


def make_engine(url: str, **kwargs: Any) -> Engine:
    """Engine with prepared statements disabled (required behind Neon's PgBouncer pooler)."""
    connect_args = {"prepare_threshold": None, **kwargs.pop("connect_args", {})}
    return create_engine(normalize_url(url), connect_args=connect_args, **kwargs)


def ping(engine: Engine, waits: Sequence[float] = DEFAULT_RETRY_WAITS_S) -> str:
    """Run `SELECT version()` with retry/backoff; return the server version string."""
    retrying = Retrying(
        stop=stop_after_attempt(len(waits) + 1),
        wait=wait_chain(*(wait_fixed(w) for w in waits)) if waits else wait_fixed(0),
        reraise=True,
    )
    for attempt in retrying:
        with attempt, engine.connect() as conn:
            return str(conn.execute(text("SELECT version()")).scalar_one())
    raise AssertionError("unreachable")  # pragma: no cover


def upgrade(url: str, revision: str = "head") -> None:
    """Run `alembic upgrade <revision>` against `url`."""
    from alembic import command
    from alembic.config import Config

    cfg = Config(str(ALEMBIC_INI))
    cfg.attributes["url"] = url
    command.upgrade(cfg, revision)
