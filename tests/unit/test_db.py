from pathlib import Path

import pytest
from typer.testing import CliRunner

from gridcast import db
from gridcast.cli import app


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("postgresql://u:p@h/d?sslmode=require", "postgresql+psycopg://u:p@h/d?sslmode=require"),
        ("postgres://u:p@h/d", "postgresql+psycopg://u:p@h/d"),
        ("postgresql+psycopg://u:p@h/d", "postgresql+psycopg://u:p@h/d"),
    ],
)
def test_normalize_url(raw: str, expected: str) -> None:
    assert db.normalize_url(raw) == expected


def test_migration_url_prefers_direct(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.chdir(tmp_path)  # keep the developer's .env out of this test
    monkeypatch.setenv("DATABASE_URL", "postgresql://pooled")
    monkeypatch.setenv("DATABASE_URL_DIRECT", "postgresql://direct")
    assert db.migration_url() == "postgresql://direct"
    monkeypatch.delenv("DATABASE_URL_DIRECT")
    assert db.migration_url() == "postgresql://pooled"


@pytest.mark.parametrize("command", [["db", "ping"], ["db", "upgrade"]])
def test_db_commands_exit_2_without_url(
    command: list[str], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.chdir(tmp_path)  # no .env here
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.delenv("DATABASE_URL_DIRECT", raising=False)
    result = CliRunner().invoke(app, command)
    assert result.exit_code == 2
