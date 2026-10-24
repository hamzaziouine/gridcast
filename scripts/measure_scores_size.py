"""Measure bytes per `scores` row on a local PostgreSQL 16 (embedded pgserver): 0001 vs 0004 layout.

Both layouts get the same rows in the order the daily job writes them: one batch per EST target
day (24 targets x 7 model names x 24 horizons = 4,032 rows), oldest day first, each batch sorted
by the table's own key. Then VACUUM ANALYZE and pg_total_relation_size (heap + indexes + TOAST).

    uv run python scripts/measure_scores_size.py --days 150
"""

from __future__ import annotations

import argparse
import tempfile
from datetime import UTC, datetime, timedelta

import numpy as np
from sqlalchemy import Engine, make_url, text

from gridcast import db

N_MODELS = 7
T0 = datetime(2026, 11, 1, 5, tzinfo=UTC)  # HE1 of an EST day


def _copy(engine: Engine, table: str, cols: list[str], rows: list[tuple[object, ...]]) -> None:
    with engine.begin() as conn:
        raw = conn.connection.driver_connection
        if raw is None:
            raise RuntimeError("no DBAPI connection")
        with raw.cursor() as cur, cur.copy(f"COPY {table} ({', '.join(cols)}) FROM STDIN") as cp:
            for r in rows:
                cp.write_row(r)


def _size(engine: Engine, table: str) -> int:
    with engine.connect() as conn:
        conn.execute(text(f"VACUUM ANALYZE {table}"))
        return int(conn.execute(text(f"SELECT pg_total_relation_size('{table}')")).scalar_one())


def day_batch(day: int, rng: np.random.Generator) -> list[tuple[datetime, int, int, float, float]]:
    """(target_time, model_id, horizon, actual, err) for the 24 targets of one EST day."""
    out = []
    for k in range(24):
        target = T0 + timedelta(days=day, hours=k)
        actual = round(float(rng.normal(16000, 2000)), 2)
        for m in range(1, N_MODELS + 1):
            for h in range(1, 25):
                out.append((target, m, h, actual, round(float(rng.normal(0, 500)), 2)))
    return out


def fill_old(engine: Engine, days: int, rng: np.random.Generator) -> int:
    names = ["champion", "shadow", "prev_champion", "seasonal_naive_168", "seasonal_naive_24",
             "persistence", "ieso_adequacy"]  # fmt: skip
    cols = [
        "issue_time", "target_time", "horizon", "model_name", "model_version", "actual_mw",
        "actual_source", "actual_revision", "err_mw", "abs_pct_err", "pinball_10", "pinball_50",
        "pinball_90", "in_80_interval", "scored_at",
    ]  # fmt: skip
    for d in range(days):
        rows = []
        for target, m, h, act, err in day_batch(d, rng):
            issue = target - timedelta(hours=h)
            rows.append(
                (issue, target, h, names[m - 1], "lgbm-2026.10.23-a3f9", act, "pub_demand", 1,
                 err, round(abs(err) / act, 5), 120.5, 230.25, 140.0, True,
                 target + timedelta(days=1))
            )  # fmt: skip
        rows.sort(key=lambda r: (r[0], r[1], r[3]))  # the 0001 PK order
        _copy(engine, "scores", cols, rows)
    return _size(engine, "scores")


def fill_new(engine: Engine, days: int, rng: np.random.Generator) -> int:
    cols = ["target_time", "model_id", "horizon", "actual_revision", "actual_mw"]
    for d in range(days):
        rows = [(t, m, h, 1, act) for t, m, h, act, _ in day_batch(d, rng)]
        rows.sort(key=lambda r: (r[0], r[1], r[2]))  # the 0004 PK order
        _copy(engine, "scores", cols, rows)
    return _size(engine, "scores")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=150)
    args = ap.parse_args()
    import pgserver  # type: ignore[import-untyped]

    n_rows = args.days * 24 * N_MODELS * 24
    out: dict[str, int] = {}
    with tempfile.TemporaryDirectory() as tmp:
        server = pgserver.get_server(tmp, cleanup_mode="stop")
        try:
            base = server.get_uri()
            admin = db.make_engine(base, isolation_level="AUTOCOMMIT")
            with admin.connect() as conn:
                ver = conn.execute(text("SHOW server_version")).scalar_one()
                for name in ("scores_old", "scores_new"):
                    conn.execute(text(f"CREATE DATABASE {name}"))
            admin.dispose()
            for name, rev, fill in (
                ("scores_old", "0003_compact_predictions", fill_old),
                ("scores_new", "0004_compact_scores", fill_new),
            ):
                url = make_url(base).set(database=name).render_as_string(hide_password=False)
                db.upgrade(url, rev)
                eng = db.make_engine(url, isolation_level="AUTOCOMMIT")
                out[rev] = fill(eng, args.days, np.random.default_rng(0))
                eng.dispose()
        finally:
            server.cleanup()
    print(f"PostgreSQL {ver}; {n_rows:,} score rows ({args.days} EST days x 24 x 7 models x 24)")
    for rev, size in out.items():
        print(f"scores after {rev}: {size / 1e6:.1f} MB, {size / n_rows:.1f} B/row")


if __name__ == "__main__":
    main()
