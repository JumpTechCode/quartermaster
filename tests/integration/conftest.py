# tests/integration/conftest.py
"""Integration-test harness: a real Postgres via testcontainers.

A single containerized Postgres is started once per session and migrated to
head; each test runs inside a transaction that is rolled back, so tests are
isolated and never see each other's writes. Everything under tests/integration/
is auto-marked 'integration' so the unit-only run (`-m 'not integration'`) stays
Docker-free. The container and migration run in a synchronous, session-scoped
fixture; the engine and connection are function-scoped so each test owns its own
event loop (no session-loop wiring needed).
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator
from pathlib import Path

import pytest
import pytest_asyncio
from alembic import command
from alembic.config import Config
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine
from testcontainers.postgres import PostgresContainer

from quartermaster.adapters.postgres.engine import create_engine


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    """Auto-mark tests under THIS directory (tests/integration/) as 'integration'.

    The hook receives the whole session's collected items, so it must scope to
    this directory's tree; otherwise unit tests collected in the same run would
    be marked integration too (and deselected by ``-m 'not integration'``).
    """
    integration_dir = Path(__file__).parent
    for item in items:
        if integration_dir in item.path.parents:
            item.add_marker("integration")


def _migrate_to_head(async_url: str) -> None:
    cfg = Config("alembic.ini")
    cfg.set_main_option("sqlalchemy.url", async_url)
    command.upgrade(cfg, "head")


@pytest.fixture(scope="session")
def postgres_url() -> Iterator[str]:
    with PostgresContainer("postgres:17") as container:
        url = str(container.get_connection_url(driver="asyncpg"))
        _migrate_to_head(url)
        yield url


@pytest_asyncio.fixture
async def engine(postgres_url: str) -> AsyncIterator[AsyncEngine]:
    eng = create_engine(postgres_url)
    try:
        yield eng
    finally:
        await eng.dispose()


@pytest_asyncio.fixture
async def db(engine: AsyncEngine) -> AsyncIterator[AsyncConnection]:
    async with engine.connect() as conn:
        trans = await conn.begin()
        try:
            yield conn
        finally:
            await trans.rollback()


_ALL_TABLES = (
    "movement",
    "reservation",
    "order_line",
    "orders",
    "receipt_line",
    "receipt",
    "stock",
    "idempotency_key",
    "sku",
    "location",
)


@pytest_asyncio.fixture
async def committed_db(engine: AsyncEngine) -> AsyncIterator[AsyncEngine]:
    """Engine for command-path tests that COMMIT; truncates all tables on teardown."""
    try:
        yield engine
    finally:
        async with engine.begin() as conn:
            await conn.execute(text(f"TRUNCATE {', '.join(_ALL_TABLES)} RESTART IDENTITY CASCADE"))
