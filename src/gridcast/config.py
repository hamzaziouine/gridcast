"""All environment-driven settings in one place (pydantic-settings)."""

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    database_url: str | None = None
    database_url_direct: str | None = None
    database_url_ro: str | None = None
    gridcast_env: str = "dev"


def get_settings() -> Settings:
    return Settings()
