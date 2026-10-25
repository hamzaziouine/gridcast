"""`gridcast status build --out DIR`: the files daily.yml force-pushes to the orphan `status`
branch (ADR-0015).

- `chart_7d.svg`: `status.chart` (7 days of day-ahead forecasts vs final actuals).
- `metrics.json`: the chart's numbers, the champion pointer fields and the newest run of each
  job (schema_version 1).
- `latest/forecast.json`, `latest/meta.json`, `latest/scoreboard.json`: a copy of the public
  snapshots the Worker serves (plan §5 "mirror"), so the record is readable without the Worker.
  A snapshot that cannot be read is left out and named in `metrics.json` `mirror_missing`.

Reads Postgres and the buckets only; writes local files only. The workflow job that pushes the
branch holds `contents: write` and no other secret.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any

from sqlalchemy import Engine, text

from gridcast.config import public_urls
from gridcast.jobs import publish
from gridcast.jobs.predict import CHAMPION_KEY
from gridcast.status import chart
from gridcast.storage.objstore import ObjectStore

SCHEMA_VERSION = 1
MIRROR = (publish.FORECAST_KEY, publish.META_KEY, publish.SCOREBOARD_KEY)
JOBS = ("hourly", "daily", "retrain", "keepalive", "gate", "prune")


def _last_runs(engine: Engine) -> dict[str, Any]:
    with engine.connect() as conn:
        rows = conn.execute(
            text(
                "SELECT DISTINCT ON (job) job, logical_time, status, run_kind, finished_at"
                " FROM pipeline_runs WHERE job = ANY(:j) AND status <> 'running'"
                " ORDER BY job, finished_at DESC NULLS LAST"
            ),
            {"j": list(JOBS)},
        ).all()
    return {
        str(r[0]): {
            "logical_time": publish.iso(r[1]),
            "status": r[2],
            "run_kind": r[3],
            "finished_at": publish.iso(r[4]),
        }
        for r in rows
    }


def build(
    engine: Engine,
    private: ObjectStore | None,
    public: ObjectStore | None,
    out: Path,
    now: datetime,
) -> dict[str, Any]:
    out.mkdir(parents=True, exist_ok=True)
    urls = public_urls()
    data = chart.load(engine, now)
    svg = chart.render_svg(data, repo_url=urls.get("repo", ""), generated_at=now)
    (out / "chart_7d.svg").write_text(svg, encoding="utf-8")
    champion: dict[str, Any] = {}
    if private is not None:
        try:
            champion = private.get_json(CHAMPION_KEY)
        except Exception:  # noqa: S110 - the chart does not need it; metrics shows null
            champion = {}
    missing: list[str] = []
    for key in MIRROR:
        try:
            if public is None:
                raise LookupError("no public store")
            body = public.get_bytes(key)
        except Exception:
            missing.append(key)
            continue
        dest = out / key
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(body)
    metrics = {
        "schema_version": SCHEMA_VERSION,
        "generated_at": publish.iso(now),
        "chart": chart.summary(data),
        "champion": {k: champion.get(k) for k in publish.CHAMPION_FIELDS},
        "last_runs": _last_runs(engine),
        "mirror_missing": missing,
        "urls": urls,
        "attribution": list(publish.ATTRIBUTION),
    }
    (out / "metrics.json").write_text(json.dumps(metrics, indent=1) + "\n", encoding="utf-8")
    readme = (
        "# GridCast status branch\n\nOrphan branch, force-pushed by `daily.yml` (ADR-0015). "
        "Generated files only; the code is on `main`.\n\n![chart](chart_7d.svg)\n"
    )
    (out / "README.md").write_text(readme, encoding="utf-8")
    files = sorted(p.relative_to(out).as_posix() for p in out.rglob("*") if p.is_file())
    return {
        "files": files,
        "svg_bytes": len(svg.encode()),
        "chart": metrics["chart"],
        "mirror_missing": missing,
    }
