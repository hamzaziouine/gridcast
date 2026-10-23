"""Compact predictions row; Adequacy3 file ledger for snapshot retention.

Revision ID: 0003_compact_predictions
Revises: 0002_weather_wide
Create Date: 2026-10-23

Why (docs/DATA_NOTES.md §9-10, §15): the 0001 predictions row measured 260 B incl. indexes, and
Adequacy3 snapshots grow ~220 MB/year; together they would push year one past Neon's 0.5 GB.

predictions (empty when this runs; it is dropped and re-created):
- MW columns `real` (4 B) instead of numeric(9,2); horizon smallint.
- model_name -> model_id smallint (lookup `prediction_models`); model_version -> version_id
  integer (lookup `model_versions`).
- run_id, run_kind and created_at are the same for the 24 rows a run writes per model, so they
  move to `prediction_runs` (one row per issue_time x model).
- target_time and ieso_staleness_min are derivable (issue_time + horizon h; data_cutoff - the
  snapshot created_at held in model_version) and are computed by the view `v_predictions`,
  which keeps the plan's §4.3 column set for every reader. 8 stored columns keep the null
  bitmap at one byte, so the tuple header stays 24 B.
- One index: the PK (issue_time, model_id, horizon). issue_time leads so every hourly insert lands
  on the right-most leaf (a model_id-first key would split mid-index pages 50/50 and leave them
  half empty: measured 130 vs 96 B/row). "Everything for target T" is issue_time BETWEEN T - 24 h
  AND T - 1 h on the PK; "latest issue of a model" is a short backward PK scan.

ieso_adequacy_files: one row per Adequacy3 file version read (created_at, archive key). The
ingest compares the listing with this ledger instead of with the snapshot rows, so a version whose
rows were pruned is never downloaded again.
"""

from alembic import op

revision: str = "0003_compact_predictions"
down_revision: str | None = "0002_weather_wide"
branch_labels: str | None = None
depends_on: str | None = None

API_RO_ROLE = "gridcast_api_ro"
MODEL_NAMES = (
    (1, "champion"),
    (2, "shadow"),
    (3, "prev_champion"),
    (4, "seasonal_naive_168"),
    (5, "seasonal_naive_24"),
    (6, "persistence"),
    (7, "ieso_adequacy"),
)

UPGRADE_SQL: list[str] = [
    "DROP TABLE predictions",
    """
    CREATE TABLE prediction_models (
      model_id   smallint PRIMARY KEY,
      model_name text NOT NULL UNIQUE
    )
    """,
    "INSERT INTO prediction_models (model_id, model_name) VALUES "  # noqa: S608 - constants
    + ", ".join(f"({i}, '{n}')" for i, n in MODEL_NAMES),
    """
    CREATE TABLE model_versions (
      version_id    integer GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
      model_version text NOT NULL UNIQUE
    )
    """,
    """
    CREATE TABLE prediction_runs (
      issue_time timestamptz NOT NULL,
      model_id   smallint    NOT NULL REFERENCES prediction_models,
      run_id     uuid        NOT NULL,
      run_kind   text        NOT NULL DEFAULT 'scheduled'
                 CHECK (run_kind IN ('scheduled','catchup','manual')),
      created_at timestamptz NOT NULL DEFAULT now(),
      PRIMARY KEY (issue_time, model_id)
    )
    """,
    # Column order: 8-byte, then 2/2/4, then 4-byte floats -> no alignment padding.
    """
    CREATE TABLE predictions (
      issue_time timestamptz NOT NULL,
      model_id   smallint    NOT NULL,
      horizon    smallint    NOT NULL CHECK (horizon BETWEEN 1 AND 24),
      version_id integer     NOT NULL REFERENCES model_versions,
      p50        real        NOT NULL,
      p10        real,
      p90        real,
      PRIMARY KEY (issue_time, model_id, horizon),
      FOREIGN KEY (issue_time, model_id) REFERENCES prediction_runs (issue_time, model_id),
      CHECK (p10 IS NULL OR p10 <= p50),
      CHECK (p90 IS NULL OR p90 >= p50)
    )
    """,
    """
    CREATE VIEW v_predictions AS
    SELECT p.issue_time,
           p.issue_time + p.horizon * interval '1 hour' AS target_time,
           p.horizon,
           m.model_name,
           v.model_version,
           p.p10, p.p50, p.p90,
           r.run_id, r.run_kind,
           CASE WHEN m.model_name = 'ieso_adequacy' THEN
             (extract(epoch FROM p.issue_time + interval '30 minutes'
                                 - v.model_version::timestamptz) / 60)::integer
           END AS ieso_staleness_min,
           r.created_at
    FROM predictions p
    JOIN prediction_models m USING (model_id)
    JOIN model_versions v USING (version_id)
    JOIN prediction_runs r USING (issue_time, model_id)
    """,
    """
    CREATE TABLE ieso_adequacy_files (
      source_file   text        PRIMARY KEY,
      delivery_date date        NOT NULL,
      version       integer     NOT NULL,
      created_at    timestamptz NOT NULL,
      fetched_at    timestamptz NOT NULL,
      archive_key   text,
      rows_kept     boolean     NOT NULL DEFAULT true
    )
    """,
    """
    INSERT INTO ieso_adequacy_files (source_file, delivery_date, version, created_at, fetched_at)
    SELECT DISTINCT ON (source_file) source_file, delivery_date, version, created_at, fetched_at
    FROM ieso_forecast_snapshots ORDER BY source_file, fetched_at
    """,
    f"GRANT SELECT ON predictions, prediction_models, model_versions, prediction_runs,"
    f" v_predictions TO {API_RO_ROLE}",
]

DOWNGRADE_SQL: list[str] = [
    "DROP TABLE ieso_adequacy_files",
    "DROP VIEW v_predictions",
    "DROP TABLE predictions",
    "DROP TABLE prediction_runs",
    "DROP TABLE model_versions",
    "DROP TABLE prediction_models",
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
      ieso_staleness_min integer,
      created_at         timestamptz NOT NULL DEFAULT now(),
      PRIMARY KEY (issue_time, target_time, model_name)
    )
    """,
    "CREATE INDEX ix_predictions_target_time ON predictions (target_time)",
    "CREATE INDEX ix_predictions_model_issue ON predictions (model_name, issue_time DESC)",
    f"GRANT SELECT ON predictions TO {API_RO_ROLE}",
]


def upgrade() -> None:
    for statement in UPGRADE_SQL:
        op.execute(statement)


def downgrade() -> None:
    for statement in DOWNGRADE_SQL:
        op.execute(statement)
