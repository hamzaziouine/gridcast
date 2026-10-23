"""Initial schema: every Core table, index, view and role of BUILD_PLAN §4.3.

Revision ID: 0001_initial
Revises:
Create Date: 2026-10-23

Notes
- All timestamps are timestamptz in UTC; target_time is the interval START of an IESO
  hour-ending block.
- Every primary key uses plain columns only (an expression such as COALESCE is not allowed in a PK).
  drift_metrics.feature is NOT NULL DEFAULT '' for that reason.
- The role gridcast_api_ro is created NOLOGIN with SELECT on the API tables only. Enabling LOGIN and
  setting its password is an operator step (secrets never live in a migration in a public repo):
      ALTER ROLE gridcast_api_ro WITH LOGIN PASSWORD '...';
"""

from alembic import op

revision: str = "0001_initial"
down_revision: str | None = None
branch_labels: str | None = None
depends_on: str | None = None

ZONE_NAMES = (
    "Northwest",
    "Northeast",
    "Ottawa",
    "East",
    "Toronto",
    "Essa",
    "Bruce",
    "Southwest",
    "Niagara",
    "West",
)
ZONES = ",".join(f"'{z}'" for z in ZONE_NAMES)

API_RO_ROLE = "gridcast_api_ro"

API_RO_TABLES = (
    "predictions",
    "scores",
    "score_daily",
    "peak_hours",
    "actuals",
    "pipeline_runs",
    "model_registry_events",
    "zone_hourly_estimates",
)

CREATE_API_RO_ROLE = """
DO $body$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'gridcast_api_ro') THEN
    CREATE ROLE gridcast_api_ro NOLOGIN;
  END IF;
END
$body$
"""

UPGRADE_SQL: list[str] = [
    # 3.1 raw demand, versioned and append-only.
    # Plan §3.0 decision 1: RealtimeTotals carries Market Demand only (hourly mean of TOTAL
    # ENERGY, complete 12-interval hours), never Ontario Demand. So ontario_demand_mw is required
    # for pub_demand rows and must be NULL for realtime_totals rows; market_demand_mw is the
    # Market Demand feature series from both sources.
    """
    CREATE TABLE raw_demand_history (
      target_time        timestamptz NOT NULL,
      source             text        NOT NULL CHECK (source IN ('pub_demand','realtime_totals')),
      file_name          text        NOT NULL,
      file_version       text,
      local_date         date        NOT NULL,
      hour_ending        smallint    NOT NULL CHECK (hour_ending BETWEEN 1 AND 24),
      market_demand_mw   numeric(9,2),
      ontario_demand_mw  numeric(9,2),
      n_intervals        smallint,
      available_at       timestamptz NOT NULL,
      available_at_kind  text        NOT NULL CHECK (available_at_kind IN ('observed','modelled')),
      fetched_at         timestamptz NOT NULL,
      http_last_modified timestamptz,
      ingested_at        timestamptz NOT NULL DEFAULT now(),
      PRIMARY KEY (target_time, source, file_name),
      CONSTRAINT ck_raw_demand_history_source_columns CHECK (
        (source = 'pub_demand' AND ontario_demand_mw IS NOT NULL)
        OR (source = 'realtime_totals' AND ontario_demand_mw IS NULL
            AND market_demand_mw IS NOT NULL AND n_intervals = 12)
      )
    )
    """,
    "CREATE INDEX ix_raw_demand_history_target_available"
    " ON raw_demand_history (target_time, available_at)",
    # 3.1b latest value per source (convenience only; as-of logic reads raw_demand_history)
    """
    CREATE VIEW raw_demand AS
    SELECT DISTINCT ON (target_time, source) *
    FROM raw_demand_history
    ORDER BY target_time, source, available_at DESC
    """,
    # 3.2 actuals = latest truth for scoring only. Plan §3.0 decision 1: provisional (realtime)
    # scoring is dropped, so the only actual source is PUB_Demand Ontario Demand.
    """
    CREATE TABLE actuals (
      target_time       timestamptz PRIMARY KEY,
      ontario_demand_mw numeric(9,2) NOT NULL,
      source            text NOT NULL DEFAULT 'pub_demand' CHECK (source = 'pub_demand'),
      revision          integer NOT NULL DEFAULT 1,
      available_at      timestamptz NOT NULL,
      available_at_kind text NOT NULL CHECK (available_at_kind IN ('observed','modelled')),
      updated_at        timestamptz NOT NULL DEFAULT now()
    )
    """,
    # 3.3 IESO Adequacy3 snapshots, every _vN version
    """
    CREATE TABLE ieso_forecast_snapshots (
      created_at    timestamptz NOT NULL,
      delivery_date date        NOT NULL,
      target_time   timestamptz NOT NULL,
      energy_mw     numeric(9,2) NOT NULL,
      version       integer     NOT NULL,
      source_file   text        NOT NULL,
      fetched_at    timestamptz NOT NULL DEFAULT now(),
      PRIMARY KEY (created_at, target_time)
    )
    """,
    "CREATE INDEX ix_ieso_forecast_snapshots_target_created"
    " ON ieso_forecast_snapshots (target_time, created_at DESC)",
    # 3.4 weather
    """
    CREATE TABLE weather_locations (
      location_id smallint PRIMARY KEY,
      name        text NOT NULL,
      lat         numeric(7,4),
      lon         numeric(7,4),
      weight      numeric(4,3)
    )
    """,
    """
    CREATE TABLE weather_hourly (
      location_id          smallint NOT NULL REFERENCES weather_locations,
      valid_time           timestamptz NOT NULL,
      temperature_2m       real,
      apparent_temperature real,
      relative_humidity_2m real,
      dew_point_2m         real,
      wind_speed_10m       real,
      cloud_cover          real,
      shortwave_radiation  real,
      precipitation        real,
      snowfall             real,
      source               text NOT NULL
                           CHECK (source IN ('historical_forecast','live_forecast_backfill')),
      available_at         timestamptz NOT NULL,
      available_at_kind    text NOT NULL CHECK (available_at_kind IN ('observed','modelled')),
      fetched_at           timestamptz NOT NULL DEFAULT now(),
      PRIMARY KEY (location_id, valid_time)
    )
    """,
    """
    CREATE TABLE weather_snapshot_index (
      issue_time   timestamptz PRIMARY KEY,
      key          text NOT NULL,
      n_rows       integer NOT NULL,
      span_start   timestamptz NOT NULL,
      span_end     timestamptz NOT NULL,
      fetched_at   timestamptz NOT NULL,
      available_at timestamptz NOT NULL
    )
    """,
    # 3.5 predictions
    """
    CREATE TABLE predictions (
      issue_time         timestamptz NOT NULL,
      target_time        timestamptz NOT NULL,
      horizon            smallint    NOT NULL CHECK (horizon BETWEEN 1 AND 24),
      model_name         text        NOT NULL,
      model_version      text        NOT NULL,
      p10                numeric(9,2),
      p50                numeric(9,2) NOT NULL,
      p90                numeric(9,2),
      run_id             uuid        NOT NULL,
      run_kind           text        NOT NULL DEFAULT 'scheduled'
                         CHECK (run_kind IN ('scheduled','catchup','manual')),
      ieso_staleness_min integer,  -- ieso_adequacy rows: issue_time - snapshot created_at, minutes
      created_at         timestamptz NOT NULL DEFAULT now(),
      PRIMARY KEY (issue_time, target_time, model_name)
    )
    """,
    "CREATE INDEX ix_predictions_target_time ON predictions (target_time)",
    "CREATE INDEX ix_predictions_model_issue ON predictions (model_name, issue_time DESC)",
    # 3.6 scores
    """
    CREATE TABLE scores (
      issue_time      timestamptz NOT NULL,
      target_time     timestamptz NOT NULL,
      horizon         smallint    NOT NULL,
      model_name      text        NOT NULL,
      model_version   text        NOT NULL,
      actual_mw       numeric(9,2) NOT NULL,
      actual_source   text        NOT NULL DEFAULT 'pub_demand'
                      CHECK (actual_source = 'pub_demand'),
      actual_revision integer     NOT NULL,
      err_mw          numeric(9,2) NOT NULL,
      abs_pct_err     numeric(8,5) NOT NULL,
      pinball_10      real,
      pinball_50      real,
      pinball_90      real,
      in_80_interval  boolean,
      scored_at       timestamptz NOT NULL DEFAULT now(),
      PRIMARY KEY (issue_time, target_time, model_name)
    )
    """,
    # 3.6b canonical peak-hour labels
    """
    CREATE TABLE peak_hours (
      target_time  timestamptz PRIMARY KEY,
      threshold_mw numeric(9,2) NOT NULL,
      is_peak      boolean NOT NULL,
      ref_start    date NOT NULL,
      ref_end      date NOT NULL,
      computed_at  timestamptz NOT NULL DEFAULT now()
    )
    """,
    # 3.7 daily rollup
    """
    CREATE TABLE score_daily (
      score_date   date NOT NULL,
      model_name   text NOT NULL,
      horizon_band text NOT NULL,
      final_only   boolean NOT NULL,
      n            integer NOT NULL,
      mape         numeric(8,5),
      rmse         numeric(9,3),
      coverage_80  numeric(6,4),
      winkler_80   numeric(10,3),
      peak_mape    numeric(8,5),
      n_peak       integer,
      computed_at  timestamptz NOT NULL DEFAULT now(),
      PRIMARY KEY (score_date, model_name, horizon_band, final_only)
    )
    """,
    # 3.8 drift (feature is NOT NULL DEFAULT '' so the PK uses plain columns only)
    """
    CREATE TABLE drift_metrics (
      run_time     timestamptz NOT NULL,
      window_start timestamptz NOT NULL,
      window_end   timestamptz NOT NULL,
      metric       text NOT NULL,
      feature      text NOT NULL DEFAULT '',
      value        numeric(12,6) NOT NULL,
      threshold    numeric(12,6) NOT NULL,
      breached     boolean NOT NULL,
      context      jsonb,
      PRIMARY KEY (run_time, metric, feature)
    )
    """,
    # 3.9 model registry events (append-only decision log)
    """
    CREATE TABLE model_registry_events (
      event_id      bigserial PRIMARY KEY,
      event_time    timestamptz NOT NULL DEFAULT now(),
      event_type    text NOT NULL CHECK (event_type IN (
                      'trained','backtested','candidate_registered','candidate_queued',
                      'shadow_started','promoted','rejected','rolled_back','interval_recalibrated')),
      model_name    text NOT NULL,
      model_version text NOT NULL,
      mlflow_run_id text,
      artifact_key  text,
      metrics       jsonb,
      reason        text NOT NULL,
      triggered_by  text NOT NULL
    )
    """,
    # 3.10 pipeline runs
    """
    CREATE TABLE pipeline_runs (
      run_id        uuid PRIMARY KEY,
      job           text NOT NULL,
      logical_time  timestamptz NOT NULL,
      run_kind      text NOT NULL DEFAULT 'scheduled'
                    CHECK (run_kind IN ('scheduled','catchup','manual')),
      started_at    timestamptz NOT NULL,
      finished_at   timestamptz,
      status        text NOT NULL CHECK (status IN ('running','ok','skipped','failed')),
      rows_written  integer DEFAULT 0,
      github_run_id text,
      notes         text
    )
    """,
    "CREATE UNIQUE INDEX pipeline_runs_one_ok ON pipeline_runs (job, logical_time)"
    " WHERE status = 'ok'",
    # 3.11 training base view
    """
    CREATE VIEW v_training_base AS
    SELECT a.target_time, a.ontario_demand_mw,
           w.location_id, w.temperature_2m, w.apparent_temperature, w.relative_humidity_2m,
           w.dew_point_2m, w.wind_speed_10m, w.cloud_cover, w.shortwave_radiation,
           w.precipitation, w.snowfall,
           w.available_at AS weather_available_at
    FROM actuals a
    JOIN weather_hourly w ON w.valid_time = a.target_time
    """,
    # 3.12 zonal actuals (demo)
    f"""
    CREATE TABLE zonal_actuals (
      target_time       timestamptz NOT NULL,
      zone              text        NOT NULL CHECK (zone IN ({ZONES})),
      demand_mw         numeric(9,2) NOT NULL,
      zone_total_mw     numeric(9,2) NOT NULL,
      ontario_demand_mw numeric(9,2) NOT NULL,
      share             numeric(7,5)
                        GENERATED ALWAYS AS (demand_mw / NULLIF(zone_total_mw, 0)) STORED,
      flagged           boolean     NOT NULL DEFAULT false,
      file_name         text        NOT NULL,
      available_at      timestamptz NOT NULL,
      ingested_at       timestamptz NOT NULL DEFAULT now(),
      PRIMARY KEY (target_time, zone)
    )
    """,
    "CREATE INDEX ix_zonal_actuals_zone_target ON zonal_actuals (zone, target_time DESC)",
    # 3.13 zone totals used by the demo, with provenance
    f"""
    CREATE TABLE zone_hourly_estimates (
      target_time     timestamptz NOT NULL,
      zone            text        NOT NULL CHECK (zone IN ({ZONES})),
      mw              numeric(9,2) NOT NULL,
      source          text        NOT NULL
                      CHECK (source IN ('zonal_actual','live_share_estimate',
                                        'forecast_share_estimate')),
      share_used      numeric(7,5),
      ontario_mw      numeric(9,2) NOT NULL,
      gta_share       numeric(7,5) NOT NULL,
      gta_mw          numeric(9,2) NOT NULL,
      offmap_mw       numeric(9,2) NOT NULL,
      gta_share_basis text        NOT NULL,
      computed_at     timestamptz NOT NULL DEFAULT now(),
      PRIMARY KEY (target_time, zone)
    )
    """,
    # Read-only API role. Roles are cluster-wide, so it is created only if absent
    # (re-runnable, and safe on a Neon branch that already carries the role).
    CREATE_API_RO_ROLE,
    f"GRANT USAGE ON SCHEMA public TO {API_RO_ROLE}",
    f"GRANT SELECT ON {', '.join(API_RO_TABLES)} TO {API_RO_ROLE}",
]

DOWNGRADE_SQL: list[str] = [
    f"REVOKE ALL ON {', '.join(API_RO_TABLES)} FROM {API_RO_ROLE}",
    f"REVOKE USAGE ON SCHEMA public FROM {API_RO_ROLE}",
    "DROP VIEW v_training_base",
    "DROP VIEW raw_demand",
    "DROP TABLE zone_hourly_estimates",
    "DROP TABLE zonal_actuals",
    "DROP TABLE pipeline_runs",
    "DROP TABLE model_registry_events",
    "DROP TABLE drift_metrics",
    "DROP TABLE score_daily",
    "DROP TABLE peak_hours",
    "DROP TABLE scores",
    "DROP TABLE predictions",
    "DROP TABLE weather_snapshot_index",
    "DROP TABLE weather_hourly",
    "DROP TABLE weather_locations",
    "DROP TABLE ieso_forecast_snapshots",
    "DROP TABLE actuals",
    "DROP TABLE raw_demand_history",
    # The role itself is left in place: roles are cluster-wide and it may hold grants in other
    # databases of the same cluster (several test databases share one local server).
]


def upgrade() -> None:
    for statement in UPGRADE_SQL:
        op.execute(statement)


def downgrade() -> None:
    for statement in DOWNGRADE_SQL:
        op.execute(statement)
