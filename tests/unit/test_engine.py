"""Unit tests for the async engine factory (no real connection)."""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncEngine

from quartermaster.adapters.postgres.engine import create_engine


async def test_create_engine_returns_async_engine_for_the_url() -> None:
    engine = create_engine("postgresql+asyncpg://u:p@localhost:5432/db")
    try:
        assert isinstance(engine, AsyncEngine)
        assert engine.url.drivername == "postgresql+asyncpg"
        assert engine.url.database == "db"
    finally:
        await engine.dispose()
