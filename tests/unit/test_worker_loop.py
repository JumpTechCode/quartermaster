"""Unit tests for the generic polled-worker loop driver."""

from __future__ import annotations

import asyncio

import pytest

from quartermaster.workers.loop import ReaperRun, run_forever


def test_reaper_run_defaults() -> None:
    assert ReaperRun() == ReaperRun(scanned=0, acted=0, reopened=0, errors=0)
    assert ReaperRun(scanned=3, acted=2, errors=1).acted == 2
    assert ReaperRun(reopened=2).reopened == 2


async def test_run_forever_runs_until_stop() -> None:
    stop = asyncio.Event()
    ticks = 0
    calls = 0

    async def tick() -> None:
        nonlocal ticks
        ticks += 1

    async def sleep(_: float) -> None:
        nonlocal calls
        calls += 1
        if calls >= 3:
            stop.set()

    await run_forever(tick, interval=0, sleep=sleep, stop=stop)
    assert ticks == 3


async def test_run_forever_survives_throwing_tick() -> None:
    stop = asyncio.Event()
    calls = 0

    async def tick() -> None:
        raise RuntimeError("boom")

    async def sleep(_: float) -> None:
        nonlocal calls
        calls += 1
        if calls >= 2:
            stop.set()

    await run_forever(tick, interval=0, sleep=sleep, stop=stop)  # must not raise
    assert calls == 2


async def test_run_forever_without_stop_loops_until_sleep_raises() -> None:
    ticks = 0

    class _Halt(Exception):
        pass

    async def tick() -> None:
        nonlocal ticks
        ticks += 1

    async def sleep(_: float) -> None:
        if ticks >= 2:
            raise _Halt

    with pytest.raises(_Halt):
        await run_forever(tick, interval=0, sleep=sleep)  # stop=None branch
    assert ticks == 2
