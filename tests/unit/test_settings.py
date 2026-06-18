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
