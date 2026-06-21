"""The idempotency-key reaper: a polled, bounded batch delete.

Removes keys older than the 24-hour retention window so the unique-index INSERT
that serialises duplicate commands stays the fast serialization point rather than
a degrading bottleneck (design §5.5). Each batch is its own transaction; the pass
loops until a short batch signals the backlog is drained, bounded so it always
terminates.
"""

from __future__ import annotations

from datetime import timedelta

from quartermaster.application.clock import Clock
from quartermaster.application.ports import UnitOfWorkFactory
from quartermaster.workers.loop import ReaperRun

_MAX_BATCHES = 1000  # safety bound so a single pass always terminates


async def reap_idempotency_keys(
    uow_factory: UnitOfWorkFactory,
    *,
    now: Clock,
    ttl: timedelta,
    batch_size: int,
) -> ReaperRun:
    """Delete keys past ``ttl`` in bounded batches; return the total removed."""
    cutoff = now() - ttl
    total = 0
    for _ in range(_MAX_BATCHES):
        async with uow_factory() as uow:
            deleted = await uow.idempotency.delete_expired(cutoff, batch_size)
            await uow.commit()
        total += deleted
        if deleted < batch_size:
            break
    return ReaperRun(acted=total)
