"""Async concurrency driver and OCC-retry instrumentation for the harness.

Each command runs with its own counting ``sleep``: the envelope calls
``await sleep(...)`` exactly once per OCC retry (and never after the final
attempt), so the call count *is* the retry count — captured without any envelope
change (design spec §5). Outcomes are classified with the envelope's own
``HARD_REJECTION``/``TRANSIENT`` tuples so the harness taxonomy can never drift
from the engine's.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from time import perf_counter
from typing import Any

from loadtest.metrics import CommandSample, Outcome
from quartermaster.application.envelope import HARD_REJECTION, TRANSIENT
from quartermaster.application.errors import RetryExhausted

Sleep = Callable[[float], Awaitable[None]]
Rand = Callable[[], float]
CommandThunk = Callable[[Sleep, Rand], Awaitable[Any]]


async def run_one(thunk: CommandThunk, rand: Rand) -> CommandSample:
    """Run one command, timing it and counting its OCC retries."""
    retries = 0

    async def counting_sleep(delay: float) -> None:
        nonlocal retries
        retries += 1
        await asyncio.sleep(delay)

    start = perf_counter()
    try:
        await thunk(counting_sleep, rand)
        outcome = Outcome.OK
    except RetryExhausted:
        outcome = Outcome.RETRY_EXHAUSTED
    except Exception as exc:
        # Deliberately total: classify with the envelope's own tuples, never crash
        # the run. isinstance(exc, <tuple>) keeps this mypy-clean (no except-var).
        if isinstance(exc, TRANSIENT):
            outcome = Outcome.TRANSIENT
        elif isinstance(exc, HARD_REJECTION):
            outcome = Outcome.REJECTED
        else:
            outcome = Outcome.ERROR
    latency = perf_counter() - start
    return CommandSample(outcome=outcome, latency_s=latency, retries=retries)


async def drive(
    thunks: list[CommandThunk], *, concurrency: int, rand: Rand
) -> tuple[list[CommandSample], float]:
    """Run all thunks with at most ``concurrency`` in flight; return samples + wall."""
    semaphore = asyncio.Semaphore(concurrency)

    async def guarded(thunk: CommandThunk) -> CommandSample:
        async with semaphore:
            return await run_one(thunk, rand)

    start = perf_counter()
    samples = await asyncio.gather(*(guarded(t) for t in thunks))
    wall = perf_counter() - start
    return list(samples), wall
