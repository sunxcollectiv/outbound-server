from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str = "postgresql+psycopg://outbound:outbound@127.0.0.1:4930/outbound"
    google_maps_api_key: str | None = None
    # Google forbids caching Place content beyond 30 days, so a cached photo
    # reference older than this is treated as expired (cover_photo.is_stale).
    google_photo_max_age_days: int = 30
    # Refresh proactively once a cache is older than this — kept under the cap
    # above so references are renewed before they actually expire. Drives which
    # places the itinerary flags for refresh and what the re-enrich script picks.
    google_cache_refresh_days: int = 25
    # Unsplash gives us curated area photos. We hotlink the image URLs (their
    # terms expect it), so no key is needed to serve them — only to fetch new
    # ones (the backfill script). The app name tags attribution links per
    # Unsplash's guidelines (utm_source).
    unsplash_access_key: str | None = None
    unsplash_app_name: str = "outbound"
    # Resend's inbound email API. Travel confirmations forwarded to a Resend
    # inbound address are pulled from here and parsed into trip items.
    resend_api_key: str | None = None
    # Anthropic's Claude API. Used to parse forwarded travel confirmation
    # emails into structured trip items.
    anthropic_api_key: str | None = None
    access_token_ttl_days: int = 90


@lru_cache
def get_settings() -> Settings:
    return Settings()
