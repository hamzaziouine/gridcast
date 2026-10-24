"""P0.3 + 0002 + 0003 + 0004: migrations apply cleanly to PostgreSQL 16, re-run and round-trip."""

from __future__ import annotations

import uuid
from datetime import UTC
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import Connection, text
from sqlalchemy.exc import IntegrityError
from typer.testing import CliRunner

from gridcast import db
from gridcast.cli import app

pytestmark = pytest.mark.integration

TABLES = {
    "raw_demand_history",
    "actuals",
    "ieso_forecast_snapshots",
    "weather_locations",
    "weather_hourly",
    "weather_snapshot_index",
    "predictions",
    "scores",
    "peak_hours",
    "score_daily",
    "drift_metrics",
    "model_registry_events",
    "pipeline_runs",
    "zonal_actuals",
    "zone_hourly_estimates",
    "prediction_models",
    "model_versions",
    "prediction_runs",
    "ieso_adequacy_files",
}
VIEWS = {"raw_demand", "v_training_base", "v_predictions", "v_scores"}
API_RO_TABLES = {
    "predictions",
    "scores",
    "score_daily",
    "peak_hours",
    "actuals",
    "pipeline_runs",
    "model_registry_events",
    "zone_hourly_estimates",
    "prediction_models",
    "model_versions",
    "prediction_runs",
}
INDEXES = {
    "ix_raw_demand_history_target_available",
    "ix_ieso_forecast_snapshots_target_created",
    "pipeline_runs_one_ok",
    "ix_zonal_actuals_zone_target",
}


def _cfg(url: str) -> Config:
    cfg = Config(str(db.ALEMBIC_INI))
    cfg.attributes["url"] = url
    cfg.attributes["configure_logger"] = False
    return cfg


def _relations(conn: Connection, kind: str) -> set[str]:
    rows = conn.execute(
        text(
            "SELECT c.relname FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace"
            " WHERE n.nspname = 'public' AND c.relkind = :kind"
        ),
        {"kind": kind},
    )
    return {r[0] for r in rows}


@pytest.fixture(scope="module")
def migrated_url(fresh_db_url: str) -> str:
    command.upgrade(_cfg(fresh_db_url), "head")
    return fresh_db_url


def test_all_objects_exist(migrated_url: str) -> None:
    engine = db.make_engine(migrated_url)
    with engine.connect() as conn:
        assert _relations(conn, "r") >= TABLES
        assert _relations(conn, "v") >= VIEWS
        assert _relations(conn, "i") >= INDEXES
        version = conn.execute(text("SELECT version_num FROM alembic_version")).scalar_one()
        assert version == "0004_compact_scores"
        server = conn.execute(text("SHOW server_version_num")).scalar_one()
        assert int(server) >= 160000
    engine.dispose()


def test_api_ro_role_is_select_only(migrated_url: str) -> None:
    engine = db.make_engine(migrated_url)
    with engine.connect() as conn:
        for table in TABLES:
            can_select = conn.execute(
                text("SELECT has_table_privilege('gridcast_api_ro', :t, 'SELECT')"), {"t": table}
            ).scalar_one()
            assert can_select is (table in API_RO_TABLES), table
            for priv in ("INSERT", "UPDATE", "DELETE", "TRUNCATE"):
                assert not conn.execute(
                    text("SELECT has_table_privilege('gridcast_api_ro', :t, :p)"),
                    {"t": table, "p": priv},
                ).scalar_one(), (table, priv)
    engine.dispose()


def test_upgrade_is_rerunnable(migrated_url: str) -> None:
    command.upgrade(_cfg(migrated_url), "head")  # already at head: a no-op that must not raise


def test_pipeline_runs_only_one_ok_per_job_and_logical_time(migrated_url: str) -> None:
    engine = db.make_engine(migrated_url)
    insert = text(
        "INSERT INTO pipeline_runs (run_id, job, logical_time, started_at, status)"
        " VALUES (:id, 'hourly', '2026-11-23 15:00Z', now(), :status)"
    )
    with engine.begin() as conn:
        for status in ("ok", "skipped", "skipped", "failed"):
            conn.execute(insert, {"id": uuid.uuid4(), "status": status})
    with pytest.raises(IntegrityError), engine.begin() as conn:
        conn.execute(insert, {"id": uuid.uuid4(), "status": "ok"})
    engine.dispose()


def test_drift_metrics_feature_defaults_to_empty_string(migrated_url: str) -> None:
    engine = db.make_engine(migrated_url)
    insert = text(
        "INSERT INTO drift_metrics (run_time, window_start, window_end, metric, value,"
        " threshold, breached) VALUES ('2026-11-23Z', '2026-11-16Z', '2026-11-23Z',"
        " 'rolling_mape_7d', 3.1, 4.0, false)"
    )
    with engine.begin() as conn:
        conn.execute(insert)
        assert conn.execute(text("SELECT feature FROM drift_metrics")).scalar_one() == ""
    with pytest.raises(IntegrityError), engine.begin() as conn:
        conn.execute(insert)  # same (run_time, metric, '') key
    engine.dispose()


def test_zonal_share_is_generated(migrated_url: str) -> None:
    engine = db.make_engine(migrated_url)
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO zonal_actuals (target_time, zone, demand_mw, zone_total_mw,"
                " ontario_demand_mw, file_name, available_at) VALUES"
                " ('2026-10-22 05:00Z', 'Toronto', 5000, 15000, 14880, 'f.csv', now())"
            )
        )
        share = conn.execute(text("SELECT share FROM zonal_actuals")).scalar_one()
        assert float(share) == pytest.approx(1 / 3, abs=1e-5)
    engine.dispose()


@pytest.mark.parametrize(
    ("source", "market", "ontario", "n_intervals", "ok"),
    [
        ("pub_demand", 16000, 15000, None, True),
        ("pub_demand", 16000, None, None, False),  # PUB rows must carry Ontario Demand
        ("realtime_totals", 16000, None, 12, True),  # Market Demand only (plan §3.0 decision 1)
        ("realtime_totals", 16000, 15000, 12, False),  # RealtimeTotals has no Ontario Demand
        ("realtime_totals", 16000, None, 11, False),  # partial hour
        ("realtime_totals", None, None, 12, False),
    ],
)
def test_raw_demand_history_source_columns(
    migrated_url: str,
    source: str,
    market: int | None,
    ontario: int | None,
    n_intervals: int | None,
    ok: bool,
) -> None:
    engine = db.make_engine(migrated_url)
    insert = text(
        "INSERT INTO raw_demand_history (target_time, source, file_name, local_date, hour_ending,"
        " market_demand_mw, ontario_demand_mw, n_intervals, available_at, available_at_kind,"
        " fetched_at) VALUES (now(), :source, :file, '2026-10-22', 1, :market, :ontario, :n,"
        " now(), 'observed', now())"
    )
    params = {
        "source": source,
        "file": f"f-{uuid.uuid4().hex}",
        "market": market,
        "ontario": ontario,
        "n": n_intervals,
    }
    if ok:
        with engine.begin() as conn:
            conn.execute(insert, params)
    else:
        with pytest.raises(IntegrityError), engine.begin() as conn:
            conn.execute(insert, params)
    engine.dispose()


def test_cli_db_ping_and_upgrade(
    migrated_url: str, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # Run from an empty directory: Settings reads `.env` from the cwd, and a developer's .env
    # holds the real Neon DATABASE_URL_DIRECT, which `db upgrade` prefers. Tests never touch Neon.
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("DATABASE_URL", migrated_url)
    monkeypatch.setenv("DATABASE_URL_DIRECT", migrated_url)
    runner = CliRunner()
    ping = runner.invoke(app, ["db", "ping", "--no-retry"])
    assert ping.exit_code == 0, ping.output
    assert "PostgreSQL" in ping.output
    upgrade = runner.invoke(app, ["db", "upgrade"])
    assert upgrade.exit_code == 0, upgrade.output


def test_v_predictions_restores_the_plan_columns(migrated_url: str) -> None:
    engine = db.make_engine(migrated_url)
    run = uuid.uuid4()
    with engine.begin() as conn:
        vid = conn.execute(
            text(
                "INSERT INTO model_versions (model_version)"
                " VALUES ('2026-10-23T20:35:02+00:00') RETURNING version_id"
            )
        ).scalar_one()
        conn.execute(
            text(
                "INSERT INTO prediction_runs (issue_time, model_id, run_id, run_kind)"
                " VALUES ('2026-10-23 21:00Z', 7, :run, 'manual')"
            ),
            {"run": run},
        )
        conn.execute(
            text(
                "INSERT INTO predictions (issue_time, model_id, horizon, version_id, p50)"
                " VALUES ('2026-10-23 21:00Z', 7, 3, :v, 15123.5)"
            ),
            {"v": vid},
        )
        row = conn.execute(text("SELECT * FROM v_predictions")).mappings().one()
    assert str(row["target_time"].astimezone(UTC)) == "2026-10-24 00:00:00+00:00"
    assert row["model_name"] == "ieso_adequacy" and row["run_id"] == run
    assert row["ieso_staleness_min"] == 55  # 21:30Z cutoff - 20:35:02Z snapshot
    assert row["p50"] == pytest.approx(15123.5)
    with pytest.raises(IntegrityError), engine.begin() as conn:  # p10 above p50
        conn.execute(
            text(
                "INSERT INTO predictions (issue_time, model_id, horizon, version_id, p50, p10)"
                " VALUES ('2026-10-23 21:00Z', 7, 4, :v, 100, 200)"
            ),
            {"v": vid},
        )
    engine.dispose()


def test_v_scores_derives_the_plan_columns(migrated_url: str) -> None:
    engine = db.make_engine(migrated_url)
    with engine.begin() as conn:
        vid = conn.execute(
            text(
                "INSERT INTO model_versions (model_version) VALUES ('lgbm-score-test')"
                " ON CONFLICT (model_version) DO UPDATE SET model_version = EXCLUDED.model_version"
                " RETURNING version_id"
            )
        ).scalar_one()
        conn.execute(
            text(
                "INSERT INTO prediction_runs (issue_time, model_id, run_id, run_kind)"
                " VALUES ('2026-11-01 10:00Z', 1, :run, 'scheduled')"
            ),
            {"run": uuid.uuid4()},
        )
        conn.execute(
            text(
                "INSERT INTO predictions (issue_time, model_id, horizon, version_id, p50, p10, p90)"
                " VALUES ('2026-11-01 10:00Z', 1, 5, :v, 15000, 14000, 16000)"
            ),
            {"v": vid},
        )
        conn.execute(
            text(
                "INSERT INTO scores (target_time, model_id, horizon, actual_revision, actual_mw)"
                " VALUES ('2026-11-01 15:00Z', 1, 5, 2, 16500)"
            )
        )
        row = conn.execute(text("SELECT * FROM v_scores")).mappings().one()
    assert str(row["issue_time"].astimezone(UTC)) == "2026-11-01 10:00:00+00:00"
    assert row["model_name"] == "champion" and row["model_version"] == "lgbm-score-test"
    assert row["actual_source"] == "pub_demand" and row["actual_revision"] == 2
    assert row["err_mw"] == pytest.approx(-1500)
    assert row["abs_pct_err"] == pytest.approx(1500 / 16500)
    assert row["pinball_50"] == pytest.approx(750)
    assert row["pinball_90"] == pytest.approx(0.9 * 500)
    assert row["pinball_10"] == pytest.approx(0.1 * 2500)
    assert row["in_80_interval"] is False
    engine.dispose()


def test_downgrade_then_upgrade_round_trips(migrated_url: str) -> None:
    # Last in the module: it empties the tables the other tests wrote to.
    cfg = _cfg(migrated_url)
    command.downgrade(cfg, "base")
    engine = db.make_engine(migrated_url)
    with engine.connect() as conn:
        assert not (_relations(conn, "r") & TABLES)
        assert not (_relations(conn, "v") & VIEWS)
    command.upgrade(cfg, "head")
    with engine.connect() as conn:
        assert _relations(conn, "r") >= TABLES
    engine.dispose()
