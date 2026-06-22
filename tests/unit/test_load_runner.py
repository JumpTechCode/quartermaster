"""The runner classifies outcomes and counts OCC retries via the sleep seam."""

from __future__ import annotations

import asyncio

from loadtest.metrics import Outcome
from loadtest.runner import Rand, Sleep, drive, run_one

from quartermaster.application.errors import RetryExhausted
from quartermaster.domain.errors import InsufficientStock
from quartermaster.domain.ids import IdempotencyKey


async def test_run_one_counts_retries_from_sleep_calls() -> None:
    calls = {"n": 0}

    async def thunk(sleep: Sleep, rand: Rand) -> None:
        # Simulate the envelope sleeping once per retry, then succeeding.
        await sleep(0.0)
        await sleep(0.0)
        calls["n"] += 1

    sample = await run_one(thunk, rand=lambda: 0.0)
    assert sample.outcome is Outcome.OK
    assert sample.retries == 2


async def test_run_one_classifies_exceptions() -> None:
    async def exhausted(sleep: Sleep, rand: Rand) -> None:
        raise RetryExhausted(IdempotencyKey("k"))

    async def transient(sleep: Sleep, rand: Rand) -> None:
        raise InsufficientStock("nope")

    async def boom(sleep: Sleep, rand: Rand) -> None:
        raise ValueError("unexpected")

    assert (await run_one(exhausted, lambda: 0.0)).outcome is Outcome.RETRY_EXHAUSTED
    assert (await run_one(transient, lambda: 0.0)).outcome is Outcome.TRANSIENT
    assert (await run_one(boom, lambda: 0.0)).outcome is Outcome.ERROR


async def test_drive_runs_all_under_concurrency_cap() -> None:
    live = {"n": 0, "peak": 0}

    async def thunk(sleep: Sleep, rand: Rand) -> None:
        live["n"] += 1
        live["peak"] = max(live["peak"], live["n"])
        await asyncio.sleep(0.01)
        live["n"] -= 1

    samples, wall = await drive([thunk for _ in range(10)], concurrency=3, rand=lambda: 0.0)
    assert len(samples) == 10
    assert all(s.outcome is Outcome.OK for s in samples)
    assert live["peak"] <= 3
    assert wall > 0.0
