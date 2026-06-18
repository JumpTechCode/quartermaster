# tests/integration/test_schema_drift.py
"""The migrated schema matches the Core metadata (no drift).

Guards every future, hand-written migration against silently diverging from
tables.py. For the initial revision (which builds the metadata) the diff is
trivially empty; the test exists to fail loudly when that stops being true.
"""

from __future__ import annotations

from alembic.autogenerate import compare_metadata
from alembic.runtime.migration import MigrationContext
from sqlalchemy import Connection
from sqlalchemy.ext.asyncio import AsyncConnection

from quartermaster.adapters.postgres.tables import metadata


async def test_no_drift_between_metadata_and_migrated_schema(db: AsyncConnection) -> None:
    def _diff(sync_conn: Connection) -> list[object]:
        context = MigrationContext.configure(sync_conn)
        return list(compare_metadata(context, metadata))

    diffs = await db.run_sync(_diff)
    assert diffs == []
