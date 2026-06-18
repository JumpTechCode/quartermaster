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
