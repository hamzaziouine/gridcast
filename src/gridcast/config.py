"""All environment-driven settings and model-wide constants in one place."""

from datetime import timedelta

from pydantic_settings import BaseSettings, SettingsConfigDict

# Modelled availability lag of a RealtimeTotals hour: available_at = target_time + REALTIME_LAG_H.
# The plan's default is 2. P0.5 (docs/DATA_NOTES.md §1.4) measured complete files posted 53-57 min
# after target_time and set a PROVISIONAL 1; it falls back to 2 if the full-day observation shows
# late or back-filled hours. Must be final before C2 writes any modelled stamp.
REALTIME_LAG_H = 1

# 80% interval coverage band: the bootstrap check (C8) and the gate use it (BUILD_PLAN §4.5, §4.7).
COVERAGE_BAND = (0.76, 0.84)

# data_cutoff = issue_time + DATA_CUTOFF_OFFSET for every source's as-of filter (BUILD_PLAN §4.6).
DATA_CUTOFF_OFFSET = timedelta(minutes=30)


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    database_url: str | None = None
    database_url_direct: str | None = None
    database_url_ro: str | None = None
    gridcast_env: str = "dev"


def get_settings() -> Settings:
    return Settings()
