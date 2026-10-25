"""config/urls.toml is the single source of public URLs: README and the Worker must agree."""

from __future__ import annotations

from pathlib import Path

from gridcast.config import public_urls

ROOT = Path(__file__).resolve().parents[2]


def test_urls_toml_loads_and_drops_empty_values() -> None:
    urls = public_urls()
    assert urls["api"].startswith("https://") and "demo_site" not in urls
    assert urls["status_chart"].endswith("/status/chart_7d.svg")


def test_readme_uses_the_configured_urls() -> None:
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    for key in ("api", "status_chart", "mlflow"):
        assert public_urls()[key] in readme, key


def test_worker_source_link_matches_the_repo() -> None:
    src = (ROOT / "infra" / "worker" / "src" / "index.js").read_text(encoding="utf-8")
    assert public_urls()["repo"] in src
