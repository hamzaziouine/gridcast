"""Measure bytes per predictions row on a local PostgreSQL 16 (embedded pgserver), old vs new.

Fills the 0002 layout (0001 predictions table) and the 0003 layout with the same synthetic
year-like batch (7 model names x 24 horizons x N issue times, realistic values), runs VACUUM
ANALYZE and reports pg_total_relation_size (heap + indexes + TOAST) per prediction row. For 0003
the per-run table (prediction_runs) and the version lookup are included, amortised per row.

    uv run python scripts/measure_predictions_size.py --issues 6000
"""

from __future__ import annotations

import argparse
import tempfile
import uuid
from datetime import UTC, datetime, timedelta

import numpy as np
from sqlalchemy import Engine, make_url, text

from gridcast import db

MODELS = (
    "champion",
    "shadow",
    "prev_champion",
    "seasonal_naive_168",
    "seasonal_naive_24",
    "persistence",
    "ieso_adequacy",
)
T0 = datetime(2026, 11, 1, tzinfo=UTC)


def _copy(engine: Engine, table: str, cols: list[str], rows: list[tuple[object, ...]]) -> None:
    with engine.begin() as conn:
        raw = conn.connection.driver_connection
        if raw is None:
            raise RuntimeError("no DBAPI connection")
        with raw.cursor() as cur, cur.copy(f"COPY {table} ({', '.join(cols)}) FROM STDIN") as cp:
            for r in rows:
                cp.write_row(r)


def _size(engine: Engine, tables: list[str]) -> dict[str, int]:
    with engine.connect() as conn:
        for t in tables:
            conn.execute(text(f"VACUUM ANALYZE {t}"))
        return {
            t: int(conn.execute(text(f"SELECT pg_total_relation_size('{t}')")).scalar_one())
            for t in tables
        }


def fill_old(engine: Engine, n_issues: int, rng: np.random.Generator) -> dict[str, int]:
    rows = []
    for i in range(n_issues):
        issue = T0 + timedelta(hours=i)
        for m in MODELS:
            run = uuid.uuid4()
            ieso = m == "ieso_adequacy"
            for h in range(1, 25):
                p50 = round(float(rng.normal(16000, 2000)), 2)
                rows.append(
                    (
                        issue,
                        issue + timedelta(hours=h),
                        h,
                        m,
                        "2026-10-23T20:35:02+00:00" if ieso else "lgbm-2026.10.23-a3f9",
                        None if ieso else round(p50 * 0.95, 2),
                        p50,
                        None if ieso else round(p50 * 1.05, 2),
                        run,
                        "scheduled",
                        55 if ieso else None,
                        issue + timedelta(minutes=9),
                    )
                )
    cols = [
        "issue_time",
        "target_time",
        "horizon",
        "model_name",
        "model_version",
        "p10",
        "p50",
        "p90",
        "run_id",
        "run_kind",
        "ieso_staleness_min",
        "created_at",
    ]
    _copy(engine, "predictions", cols, rows)
    return _size(engine, ["predictions"])


def fill_scores(engine: Engine, n_issues: int, rng: np.random.Generator) -> dict[str, int]:
    """The 0001 `scores` table (unchanged by 0003), filled like one model-year of scoring."""
    rows = []
    for i in range(n_issues):
        issue = T0 + timedelta(hours=i)
        for m in MODELS:
            for h in range(1, 25):
                act = round(float(rng.normal(16000, 2000)), 2)
                err = round(float(rng.normal(0, 500)), 2)
                rows.append(
                    (issue, issue + timedelta(hours=h), h, m, "lgbm-2026.10.23-a3f9", act,
                     "pub_demand", 1, err, round(abs(err) / act, 5), 120.5, 230.25, 140.0, True,
                     issue + timedelta(days=1))
                )  # fmt: skip
    cols = [
        "issue_time", "target_time", "horizon", "model_name", "model_version", "actual_mw",
        "actual_source", "actual_revision", "err_mw", "abs_pct_err", "pinball_10", "pinball_50",
        "pinball_90", "in_80_interval", "scored_at",
    ]  # fmt: skip
    _copy(engine, "scores", cols, rows)
    return _size(engine, ["scores"])


def fill_new(engine: Engine, n_issues: int, rng: np.random.Generator) -> dict[str, int]:
    with engine.begin() as conn:
        lgbm = conn.execute(
            text(
                "INSERT INTO model_versions (model_version) VALUES ('lgbm-2026.10.23-a3f9')"
                " RETURNING version_id"
            )
        ).scalar_one()
    runs, preds, versions = [], [], []
    for i in range(n_issues):
        issue = T0 + timedelta(hours=i)
        versions.append(((issue - timedelta(minutes=25)).isoformat(),))
        for mid, m in enumerate(MODELS, start=1):
            runs.append((issue, mid, uuid.uuid4(), "scheduled", issue + timedelta(minutes=9)))
            ieso = m == "ieso_adequacy"
            for h in range(1, 25):
                p50 = float(rng.normal(16000, 2000))
                vid = lgbm + 1 + i if ieso else lgbm  # one IESO snapshot version per issue hour
                preds.append(
                    (
                        issue,
                        mid,
                        h,
                        vid,
                        p50,
                        None if ieso else p50 * 0.95,
                        None if ieso else p50 * 1.05,
                    )
                )
    _copy(engine, "model_versions", ["model_version"], versions)
    _copy(
        engine,
        "prediction_runs",
        ["issue_time", "model_id", "run_id", "run_kind", "created_at"],
        runs,
    )
    cols = ["issue_time", "model_id", "horizon", "version_id", "p50", "p10", "p90"]
    _copy(engine, "predictions", cols, preds)
    return _size(engine, ["predictions", "prediction_runs", "model_versions"])


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--issues", type=int, default=6000)
    args = ap.parse_args()
    import pgserver  # type: ignore[import-untyped]

    rng = np.random.default_rng(0)
    n_rows = args.issues * len(MODELS) * 24
    with tempfile.TemporaryDirectory() as tmp:
        server = pgserver.get_server(tmp, cleanup_mode="stop")
        try:
            base = server.get_uri()
            admin = db.make_engine(base, isolation_level="AUTOCOMMIT")
            with admin.connect() as conn:
                ver = conn.execute(text("SHOW server_version")).scalar_one()
                for name in ("size_old", "size_new"):
                    conn.execute(text(f"CREATE DATABASE {name}"))
            out: dict[str, dict[str, int]] = {}
            for name, rev, fill in (
                ("size_old", "0002_weather_wide", fill_old),
                ("size_new", "0003_compact_predictions", fill_new),
            ):
                url = make_url(base).set(database=name).render_as_string(hide_password=False)
                db.upgrade(url, rev)
                eng = db.make_engine(url, isolation_level="AUTOCOMMIT")
                out[rev] = fill(eng, args.issues, rng)
                eng.dispose()
                if rev == "0003_compact_predictions":
                    eng = db.make_engine(url, isolation_level="AUTOCOMMIT")
                    out["scores (0001 layout, unchanged)"] = fill_scores(eng, args.issues, rng)
                    eng.dispose()
            admin.dispose()
        finally:
            server.cleanup()
    print(f"PostgreSQL {ver}; {n_rows:,} prediction rows ({args.issues} issues x 7 models x 24)")
    for rev, sizes in out.items():
        total = sum(sizes.values())
        parts = ", ".join(f"{t} {s / 1e6:.1f} MB" for t, s in sizes.items())
        print(f"{rev}: {total / 1e6:.1f} MB total, {total / n_rows:.1f} B/row ({parts})")


if __name__ == "__main__":
    main()
