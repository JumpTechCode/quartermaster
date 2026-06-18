# tests/integration/test_migration.py
"""The migration runs cleanly in both directions.

This test uses its own dedicated container, never the shared session database —
down-migrating to base would otherwise drop the tables the other integration
tests rely on.
"""

from __future__ import annotations

from alembic import command
from alembic.config import Config
from testcontainers.postgres import PostgresContainer


def _cfg(async_url: str) -> Config:
    cfg = Config("alembic.ini")
    cfg.set_main_option("sqlalchemy.url", async_url)
    return cfg


def test_migration_upgrade_downgrade_upgrade_is_clean() -> None:
    with PostgresContainer("postgres:17") as container:
        cfg = _cfg(str(container.get_connection_url(driver="asyncpg")))
        command.upgrade(cfg, "head")
        command.downgrade(cfg, "base")
        command.upgrade(cfg, "head")
