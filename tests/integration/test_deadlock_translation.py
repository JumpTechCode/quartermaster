# tests/integration/test_deadlock_translation.py
"""A forced ABBA deadlock on real Postgres must surface as the application-level
``OccConflict`` so the envelope's bounded OCC retry absorbs it -- not as a raw
``DeadlockDetected`` that escapes to an opaque 500 (issue #46).

The command handlers and the reservation reaper acquire row locks in opposite
orders, so a ``pick``/``cancel`` racing the reaper on the same order can form an
ABBA cycle that Postgres breaks with ``DeadlockDetected`` (SQLSTATE ``40P01``).
This test reproduces that cycle deterministically with two connections taking
``FOR UPDATE`` row locks in crossed order, independent of the document schema, so
the regression target is the adapter-boundary translation itself.
"""

from __future__ import annotations

import asyncio

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from quartermaster.application.errors import OccConflict

_LOCK_ROW = text("SELECT 1 FROM location WHERE location_id = :id FOR UPDATE")


async def _seed_two_rows(engine: AsyncEngine) -> None:
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO location (location_id, kind) "
                "VALUES ('DLK-A', 'shelf'), ('DLK-B', 'shelf')"
            )
        )


async def _hold_then_cross(
    conn: AsyncConnection,
    first: str,
    second: str,
    locked: asyncio.Event,
    go: asyncio.Event,
) -> None:
    await conn.execute(_LOCK_ROW, {"id": first})  # our own row -- no contention yet
    locked.set()
    await go.wait()  # wait until BOTH sides hold their first row
    await conn.execute(_LOCK_ROW, {"id": second})  # cross -- forms the ABBA cycle


async def test_deadlock_is_translated_to_occ_conflict(committed_db: AsyncEngine) -> None:
    engine = committed_db
    await _seed_two_rows(engine)

    conn_a = await engine.connect()
    conn_b = await engine.connect()
    trans_a = await conn_a.begin()
    trans_b = await conn_b.begin()
    a_locked, b_locked, go = asyncio.Event(), asyncio.Event(), asyncio.Event()
    try:
        task_a = asyncio.create_task(_hold_then_cross(conn_a, "DLK-A", "DLK-B", a_locked, go))
        task_b = asyncio.create_task(_hold_then_cross(conn_b, "DLK-B", "DLK-A", b_locked, go))
        await asyncio.wait_for(asyncio.gather(a_locked.wait(), b_locked.wait()), timeout=10)
        go.set()  # release both into the crossing lock -> guaranteed deadlock
        results = await asyncio.wait_for(
            asyncio.gather(task_a, task_b, return_exceptions=True), timeout=10
        )
    finally:
        # The aborted side must still roll back cleanly -- this is exactly the
        # ``uow.rollback()`` the envelope issues before it retries.
        await trans_a.rollback()
        await trans_b.rollback()
        await conn_a.close()
        await conn_b.close()

    errors = [r for r in results if isinstance(r, BaseException)]
    assert len(errors) == 1, f"exactly one side should be aborted, got: {results}"
    assert isinstance(errors[0], OccConflict), (
        f"a deadlock must translate to OccConflict, got {type(errors[0]).__name__}: {errors[0]}"
    )
