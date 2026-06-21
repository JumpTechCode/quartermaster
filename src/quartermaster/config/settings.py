"""Application settings loaded from the environment.

Minimal for the persistence slice: just the database URL the async engine needs.
The surface grows in later slices. Values come from ``QM_``-prefixed environment
variables (e.g. ``QM_DATABASE_URL``) and are validated at construction by
pydantic-settings.
"""

from __future__ import annotations

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Runtime configuration sourced from the environment."""

    model_config = SettingsConfigDict(env_prefix="QM_")

    database_url: str
    reservation_reaper_interval_s: float = 60.0
    idempotency_reaper_interval_s: float = 3600.0
    reaper_batch_size: int = 500
    idempotency_ttl_hours: int = 24
    backorder_sweep_interval_s: float = 30.0
