"""run_workers wires both reaper loops without touching a database."""

from __future__ import annotations

from collections.abc import Awaitable, Callable

import quartermaster.app as app_module
from quartermaster.app import run_workers


async def test_run_workers_schedules_both_reapers(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setenv("QM_DATABASE_URL", "postgresql+asyncpg://u:p@localhost/db")
    intervals: list[float] = []

    async def fake_run_forever(
        tick: Callable[[], Awaitable[object]],
        *,
        interval: float,
        sleep: object = None,
        stop: object = None,
    ) -> None:
        intervals.append(interval)  # record wiring; never call tick (would hit DB)

    monkeypatch.setattr(app_module, "run_forever", fake_run_forever)

    await run_workers()

    assert sorted(intervals) == [60.0, 3600.0]
