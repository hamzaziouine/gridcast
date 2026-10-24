"""Compact scores row: keyed by target, derived columns in the view `v_scores`.

Revision ID: 0004_compact_scores
Revises: 0003_compact_predictions
Create Date: 2026-10-24

Why (docs/DATA_NOTES.md §14.3, §17): the 0001 scores row measured 198.7 B incl. its PK, which
alone would put year one at ~574 MB, past Neon's 0.5 GB.

scores (empty when this runs; it is dropped and re-created):
- One stored fact per scored prediction: the actual it was scored against (`actual_mw real`) and
  that actual's `actual_revision` (smallint; a re-score is an UPDATE when actuals.revision moves).
- model_name -> model_id smallint (the `prediction_models` lookup of 0003).
- Everything else is derivable from the prediction row and this actual, so `v_scores` computes it:
  issue_time (target_time - horizon h), model_version (predictions.version_id), err_mw,
  abs_pct_err, the three pinball losses, in_80_interval. actual_source is always 'pub_demand'
  (plan §3.0 decision 1: provisional scoring dropped). scored_at is dropped (pipeline_runs has it).
- One index: the PK (target_time, model_id, horizon). target_time leads because the daily job
  scores one EST day of targets at a time, oldest day first, so every batch lands on the
  right-most leaf (an issue_time-first key would interleave consecutive batches: the issue times
  of day D's targets overlap day D-1's by 23 h).
- No FK to predictions: the view inner-joins predictions, and the 90-day prune of the weak
  baselines deletes their score rows in the same statement.
"""

from alembic import op

revision: str = "0004_compact_scores"
down_revision: str | None = "0003_compact_predictions"
branch_labels: str | None = None
depends_on: str | None = None

API_RO_ROLE = "gridcast_api_ro"

UPGRADE_SQL: list[str] = [
    "DROP TABLE scores",
    # Column order: 8-byte, then 2/2/2 (+2 pad), then the 4-byte float.
    """
    CREATE TABLE scores (
      target_time     timestamptz NOT NULL,
      model_id        smallint    NOT NULL REFERENCES prediction_models,
      horizon         smallint    NOT NULL CHECK (horizon BETWEEN 1 AND 24),
      actual_revision smallint    NOT NULL,
      actual_mw       real        NOT NULL,
      PRIMARY KEY (target_time, model_id, horizon)
    )
    """,
    """
    CREATE VIEW v_scores AS
    SELECT s.target_time - s.horizon * interval '1 hour' AS issue_time,
           s.target_time,
           s.horizon,
           m.model_name,
           v.model_version,
           s.actual_mw,
           'pub_demand'::text AS actual_source,
           s.actual_revision,
           p.p10, p.p50, p.p90,
           p.p50 - s.actual_mw AS err_mw,
           abs(p.p50 - s.actual_mw) / s.actual_mw AS abs_pct_err,
           greatest(0.1 * (s.actual_mw - p.p10), 0.9 * (p.p10 - s.actual_mw)) AS pinball_10,
           0.5 * abs(s.actual_mw - p.p50) AS pinball_50,
           greatest(0.9 * (s.actual_mw - p.p90), 0.1 * (p.p90 - s.actual_mw)) AS pinball_90,
           CASE WHEN p.p10 IS NULL OR p.p90 IS NULL THEN NULL
                ELSE s.actual_mw BETWEEN p.p10 AND p.p90 END AS in_80_interval
    FROM scores s
    JOIN predictions p
      ON p.issue_time = s.target_time - s.horizon * interval '1 hour'
     AND p.model_id = s.model_id AND p.horizon = s.horizon
    JOIN prediction_models m ON m.model_id = s.model_id
    JOIN model_versions v ON v.version_id = p.version_id
    """,
    f"GRANT SELECT ON scores, v_scores TO {API_RO_ROLE}",
]

DOWNGRADE_SQL: list[str] = [
    "DROP VIEW v_scores",
    "DROP TABLE scores",
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
    f"GRANT SELECT ON scores TO {API_RO_ROLE}",
]


def upgrade() -> None:
    for statement in UPGRADE_SQL:
        op.execute(statement)


def downgrade() -> None:
    for statement in DOWNGRADE_SQL:
        op.execute(statement)
