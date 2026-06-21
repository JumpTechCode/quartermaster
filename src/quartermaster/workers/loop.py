"""Shared background-worker infrastructure: the result record and the poll loop.

A worker's unit of work is a ``tick`` coroutine; :func:`run_forever` schedules it
on a fixed interval and keeps the loop alive across transient failures (a raised
tick is logged, not fatal). ``sleep`` and ``stop`` are seams so tests drive a
deterministic number of iterations without real time passing.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ReaperRun:
    """Telemetry for one bounded reaper pass."""

    scanned: int = 0
    acted: int = 0
    reopened: int = 0
    errors: int = 0


async def run_forever(
    tick: Callable[[], Awaitable[object]],
    *,
    interval: float,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    stop: asyncio.Event | None = None,
) -> None:
    """Run ``tick`` then sleep ``interval``, repeating until ``stop`` is set."""
    while stop is None or not stop.is_set():
        try:
            await tick()
        except Exception:
            logger.exception("worker tick failed; continuing")
        await sleep(interval)
