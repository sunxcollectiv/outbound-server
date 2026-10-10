from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str = "postgresql+psycopg://outbound:outbound@127.0.0.1:4930/outbound"
    google_maps_api_key: str | None = None
    access_token_ttl_days: int = 90


@lru_cache
def get_settings() -> Settings:
    return Settings()
