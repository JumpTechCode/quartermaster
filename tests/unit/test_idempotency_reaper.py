"""Unit tests for the idempotency-key reaper pass (fakes; no DB)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from quartermaster.workers.idempotency_reaper import reap_idempotency_keys
from tests.unit.fakes import FakeIdempotencyRepo, FakeUnitOfWork, fake_factory

_NOW = datetime(2026, 6, 20, 12, 0, tzinfo=UTC)
_TTL = timedelta(hours=24)


async def test_drains_in_batches_until_short_batch() -> None:
    idem = FakeIdempotencyRepo(delete_results=[2, 2, 1])
    uow = FakeUnitOfWork(idempotency=idem)
    run = await reap_idempotency_keys(fake_factory(uow), now=lambda: _NOW, ttl=_TTL, batch_size=2)

    assert run.acted == 5
    assert len(idem.delete_calls) == 3
    assert idem.delete_calls[0] == (_NOW - _TTL, 2)  # cutoff = now - ttl


async def test_nothing_to_delete() -> None:
    idem = FakeIdempotencyRepo(delete_results=[0])
    uow = FakeUnitOfWork(idempotency=idem)
    run = await reap_idempotency_keys(fake_factory(uow), now=lambda: _NOW, ttl=_TTL, batch_size=500)

    assert run.acted == 0
    assert len(idem.delete_calls) == 1  # one probe batch, then stop
