"""Idempotency write-path hardening on real Postgres (issue #38).

Two guarantees:

- ``finalize`` is guarded (``WHERE status = 'pending'``) and asserts it updates
  exactly one row, so a second finalize cannot silently overwrite a terminal
  record.
- The exactly-once claim rests on ``INSERT ... ON CONFLICT DO NOTHING`` blocking
  a concurrent same-key duplicate until the first transaction resolves. A forced
  interleaving locks that in: the duplicate blocks, then replays the committed
  result, or claims cleanly if the first rolled back.
"""

from __future__ import annotations

import asyncio

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from quartermaster.adapters.postgres.unit_of_work import PostgresUnitOfWork
from quartermaster.application.errors import IdempotencyFinalizeError
from quartermaster.application.ports import ClaimOutcome
from quartermaster.domain.idempotency import IdempotencyStatus
from quartermaster.domain.ids import IdempotencyKey


async def test_second_finalize_raises_rather_than_overwriting(committed_db: AsyncEngine) -> None:
    key = IdempotencyKey("finalize-guard")
    async with PostgresUnitOfWork(committed_db) as uow:
        assert await uow.idempotency.claim(key, "fp") is ClaimOutcome.CLAIMED
        await uow.idempotency.finalize(key, IdempotencyStatus.SUCCEEDED, {"value": 1})
        # The row is now terminal; a second finalize matches no pending row.
        with pytest.raises(IdempotencyFinalizeError):
            await uow.idempotency.finalize(key, IdempotencyStatus.REJECTED, {"value": 2})
        await uow.rollback()


async def test_concurrent_same_key_claim_blocks_then_replays(committed_db: AsyncEngine) -> None:
    key = IdempotencyKey("dup-commit")
    a = PostgresUnitOfWork(committed_db)
    b = PostgresUnitOfWork(committed_db)
    await a.__aenter__()
    await b.__aenter__()
    try:
        assert await a.idempotency.claim(key, "fp") is ClaimOutcome.CLAIMED  # A wins (uncommitted)

        # B's claim must block on A's uncommitted row, not return immediately.
        b_claim = asyncio.create_task(b.idempotency.claim(key, "fp"))
        await asyncio.sleep(0.2)
        assert not b_claim.done()

        await a.idempotency.finalize(key, IdempotencyStatus.SUCCEEDED, {"value": 1})
        await a.commit()  # B unblocks here

        assert await asyncio.wait_for(b_claim, timeout=5) is ClaimOutcome.EXISTS
        stored = await b.idempotency.load(key)
        assert stored is not None
        assert stored.status is IdempotencyStatus.SUCCEEDED
        assert stored.response == {"value": 1}
        await b.commit()
    finally:
        await a.__aexit__(None, None, None)
        await b.__aexit__(None, None, None)


async def test_concurrent_same_key_claim_succeeds_after_first_rolls_back(
    committed_db: AsyncEngine,
) -> None:
    key = IdempotencyKey("dup-rollback")
    a = PostgresUnitOfWork(committed_db)
    b = PostgresUnitOfWork(committed_db)
    await a.__aenter__()
    await b.__aenter__()
    try:
        assert await a.idempotency.claim(key, "fp") is ClaimOutcome.CLAIMED

        b_claim = asyncio.create_task(b.idempotency.claim(key, "fp"))
        await asyncio.sleep(0.2)
        assert not b_claim.done()

        await a.rollback()  # first attempt failed; the key must not be poisoned

        assert await asyncio.wait_for(b_claim, timeout=5) is ClaimOutcome.CLAIMED
        await b.commit()
    finally:
        await a.__aexit__(None, None, None)
        await b.__aexit__(None, None, None)
