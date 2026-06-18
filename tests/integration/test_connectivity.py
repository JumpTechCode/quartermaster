# tests/integration/test_connectivity.py
"""The harness reaches a migrated Postgres and the schema is present."""

from __future__ import annotations

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

EXPECTED_TABLES = {
    "sku",
    "location",
    "stock",
    "orders",
    "order_line",
    "receipt",
    "receipt_line",
    "reservation",
    "movement",
    "idempotency_key",
}


async def test_database_is_reachable(db: AsyncConnection) -> None:
    result = await db.execute(text("SELECT 1"))
    assert result.scalar_one() == 1


async def test_all_tables_exist_after_migration(db: AsyncConnection) -> None:
    result = await db.execute(
        text("SELECT table_name FROM information_schema.tables WHERE table_schema = 'public'")
    )
    present = {row[0] for row in result}
    assert present >= EXPECTED_TABLES
