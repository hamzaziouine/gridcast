"""Real-Postgres fixtures for integration tests.

Server selection, in order:
1. GRIDCAST_TEST_DATABASE_URL (CI: the postgres:16 service container). Deliberately NOT
   DATABASE_URL, so a developer's .env pointing at Neon is never used by tests that drop schemas.
2. The embedded `pgserver` package (dev dependency; ships PostgreSQL 16 binaries, works on Windows).
3. Otherwise every integration test is SKIPPED with the reason printed (`pytest -ra`).

Each test module gets its own freshly created database, dropped afterwards.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import Iterator

import pytest
from sqlalchemy import make_url, text

from gridcast.db import make_engine


@pytest.fixture(scope="session")
def pg_server_url(tmp_path_factory: pytest.TempPathFactory) -> Iterator[str]:
    url = os.environ.get("GRIDCAST_TEST_DATABASE_URL")
    if url:
        yield url
        return
    try:
        import pgserver  # type: ignore[import-untyped]
    except ImportError:
        pytest.skip("no GRIDCAST_TEST_DATABASE_URL and pgserver is not installed")
    data_dir = tmp_path_factory.mktemp("pgdata")
    try:
        server = pgserver.get_server(str(data_dir), cleanup_mode="stop")
    except Exception as exc:  # pragma: no cover - depends on the host
        pytest.skip(f"embedded pgserver failed to start: {exc}")
    try:
        yield server.get_uri()
    finally:
        server.cleanup()


@pytest.fixture(scope="module")
def fresh_db_url(pg_server_url: str) -> Iterator[str]:
    name = f"gridcast_test_{uuid.uuid4().hex[:12]}"
    admin = make_engine(pg_server_url, isolation_level="AUTOCOMMIT")
    with admin.connect() as conn:
        conn.execute(text(f'CREATE DATABASE "{name}"'))
    url = make_url(pg_server_url).set(database=name).render_as_string(hide_password=False)
    try:
        yield url
    finally:
        with admin.connect() as conn:
            conn.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
        admin.dispose()
