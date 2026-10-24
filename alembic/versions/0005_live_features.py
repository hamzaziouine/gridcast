"""live_features: the h = 1 drift features of every champion forecast as served.

Revision ID: 0005_live_features
Revises: 0004_compact_scores
Create Date: 2026-10-24

Why (BUILD_PLAN §6.2 step 4, §4.6 lag parity; DATA_NOTES §19.3): PSI needs the distribution of
the live feature values and the l0_lag_parity check needs the live L0 lag. The hourly job does
not keep its feature frame, so it now writes one row per issue hour with the PSI features at
horizon 1 and `mkt_l0_age_h`. One row per issue hour ≈ 8.8 k rows/year, < 1 MB.

Additive only (a new table): a job still running the previous code is unaffected.
"""

from alembic import op

revision: str = "0005_live_features"
down_revision: str | None = "0004_compact_scores"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS live_features (
          issue_time             timestamptz PRIMARY KEY,
          mkt_l0_age_h           real,
          temperature_2m_w       real,
          apparent_temperature_w real,
          dew_point_2m_w         real,
          hdd_w                  real,
          cdd_w                  real,
          shortwave_radiation_w  real,
          wind_speed_10m_w       real,
          ont_lag168             real
        )
        """
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS live_features")
