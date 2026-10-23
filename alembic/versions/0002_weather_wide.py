"""Weather store: one row per hour with a column per (variable, city); seed the 6 cities.

Revision ID: 0002_weather_wide
Revises: 0001_initial
Create Date: 2026-10-23

Why (docs/DATA_NOTES.md §9, C3 storage check): with the 2018-01-01 start (plan §3.0 decision 5)
the 0001 row-per-city-hour layout measures ~167 B/row incl. its PK, i.e. ~77 MB for 6 cities x
2018->now, and grows 8.8 MB/year. One row per hour carries the per-row header, PK entry, source,
availability stamp and fetched_at once instead of six times: ~21 MB for the same data, lossless.
It is also the feature-matrix shape (a column per city and variable), so the live snapshot frame
and the training frame share one column set.

weather_hourly is empty when this runs (C3 is its first writer), so no data is moved.
"""

from alembic import op

revision: str = "0002_weather_wide"
down_revision: str | None = "0001_initial"
branch_labels: str | None = None
depends_on: str | None = None

# (location_id, name, column key, lat, lon, weight) — BUILD_PLAN §4.5, a documented constant.
CITIES = (
    (1, "Toronto", "toronto", 43.65, -79.38, 0.45),
    (2, "Ottawa", "ottawa", 45.42, -75.70, 0.15),
    (3, "London", "london", 42.98, -81.25, 0.10),
    (4, "Windsor", "windsor", 42.30, -83.02, 0.08),
    (5, "Sudbury", "sudbury", 46.49, -80.99, 0.07),
    (6, "Thunder Bay", "thunder_bay", 48.38, -89.25, 0.05),
)
VARIABLES = (
    "temperature_2m",
    "apparent_temperature",
    "relative_humidity_2m",
    "dew_point_2m",
    "wind_speed_10m",
    "cloud_cover",
    "shortwave_radiation",
    "precipitation",
    "snowfall",
)
WEATHER_COLUMNS = [f"{var}_{key}" for _, _, key, *_ in CITIES for var in VARIABLES]

CREATE_WIDE = f"""
CREATE TABLE weather_hourly (
  valid_time        timestamptz PRIMARY KEY,
  source            text NOT NULL
                    CHECK (source IN ('historical_forecast','live_forecast_backfill')),
  available_at      timestamptz NOT NULL,
  available_at_kind text NOT NULL CHECK (available_at_kind IN ('observed','modelled')),
  fetched_at        timestamptz NOT NULL DEFAULT now(),
  {", ".join(f"{c} real" for c in WEATHER_COLUMNS)}
)
"""

# Column names below come from the constants above, never from input.
CREATE_VIEW_WIDE = f"""
CREATE VIEW v_training_base AS
SELECT a.target_time, a.ontario_demand_mw,
       {", ".join(f"w.{c}" for c in WEATHER_COLUMNS)},
       w.available_at AS weather_available_at
FROM actuals a
JOIN weather_hourly w ON w.valid_time = a.target_time
"""  # noqa: S608

CREATE_LONG = """
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
"""

CREATE_VIEW_LONG = """
CREATE VIEW v_training_base AS
SELECT a.target_time, a.ontario_demand_mw,
       w.location_id, w.temperature_2m, w.apparent_temperature, w.relative_humidity_2m,
       w.dew_point_2m, w.wind_speed_10m, w.cloud_cover, w.shortwave_radiation,
       w.precipitation, w.snowfall,
       w.available_at AS weather_available_at
FROM actuals a
JOIN weather_hourly w ON w.valid_time = a.target_time
"""

SEED = (
    "INSERT INTO weather_locations (location_id, name, lat, lon, weight) VALUES "  # noqa: S608
    + ", ".join(f"({i}, '{name}', {lat}, {lon}, {w})" for i, name, _, lat, lon, w in CITIES)
    + " ON CONFLICT (location_id) DO NOTHING"
)


def upgrade() -> None:
    op.execute("DROP VIEW v_training_base")
    op.execute("DROP TABLE weather_hourly")
    op.execute(CREATE_WIDE)
    op.execute(CREATE_VIEW_WIDE)
    op.execute(SEED)


def downgrade() -> None:
    op.execute("DROP VIEW v_training_base")
    op.execute("DROP TABLE weather_hourly")
    op.execute(CREATE_LONG)
    op.execute(CREATE_VIEW_LONG)
    op.execute("DELETE FROM weather_locations WHERE location_id BETWEEN 1 AND 6")
