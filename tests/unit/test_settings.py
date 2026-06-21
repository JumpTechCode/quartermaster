"""Unit tests for environment-sourced settings."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from quartermaster.config.settings import Settings


def test_settings_reads_database_url_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("QM_DATABASE_URL", "postgresql+asyncpg://u:p@h:5432/db")
    assert Settings().database_url == "postgresql+asyncpg://u:p@h:5432/db"


def test_settings_database_url_is_required(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("QM_DATABASE_URL", raising=False)
    with pytest.raises(ValidationError):
        Settings()


def test_reaper_settings_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("QM_DATABASE_URL", "postgresql+asyncpg://u:p@localhost/db")
    settings = Settings()
    assert settings.reservation_reaper_interval_s == 60.0
    assert settings.idempotency_reaper_interval_s == 3600.0
    assert settings.reaper_batch_size == 500
    assert settings.idempotency_ttl_hours == 24


def test_backorder_sweep_interval_default(monkeypatch: pytest.MonkeyPatch) -> None:
    from quartermaster.config.settings import Settings

    monkeypatch.setenv("QM_DATABASE_URL", "postgresql+asyncpg://u:p@localhost/db")
    assert Settings().backorder_sweep_interval_s == 30.0
