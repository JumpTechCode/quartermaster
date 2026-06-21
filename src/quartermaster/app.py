"""Composition root.

The only module permitted to import concrete adapters and wire them to the
application ports. ``build_app`` reads settings, builds the async engine and the
seam bundle, and returns the wired FastAPI app; serve it with
``uvicorn quartermaster.app:build_app --factory``. Building lazily (a factory,
not a module-level app) keeps importing this module side-effect-free, so the
boundary checker and tests can import it without a configured database.
"""

from __future__ import annotations

import asyncio
import logging
import signal
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress
from datetime import timedelta

from fastapi import FastAPI

from quartermaster.adapters.postgres.engine import create_engine
from quartermaster.adapters.postgres.identifiers import (
    new_movement_id,
    new_order_id,
    new_receipt_id,
    new_reservation_id,
)
from quartermaster.adapters.postgres.unit_of_work import postgres_uow_factory
from quartermaster.api.app import create_app
from quartermaster.api.deps import Deps
from quartermaster.application.clock import system_clock
from quartermaster.config.settings import Settings
from quartermaster.workers.backorder_sweep import sweep_backorders
from quartermaster.workers.idempotency_reaper import reap_idempotency_keys
from quartermaster.workers.loop import run_forever
from quartermaster.workers.reservation_reaper import reap_reservations


def build_app() -> FastAPI:
    """Assemble the production app: engine, seams, routes, and lifecycle."""
    settings = Settings()
    engine = create_engine(settings.database_url)
    deps = Deps(
        uow_factory=postgres_uow_factory(engine),
        now=system_clock,
        new_order_id=new_order_id,
        new_receipt_id=new_receipt_id,
        new_reservation_id=new_reservation_id,
        new_movement_id=new_movement_id,
    )

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        yield
        await engine.dispose()

    return create_app(deps, lifespan=lifespan)


async def run_workers() -> None:
    """Run the polled background reapers until SIGTERM/SIGINT (the worker process)."""
    logging.basicConfig(level=logging.INFO)
    settings = Settings()
    engine = create_engine(settings.database_url)
    factory = postgres_uow_factory(engine)
    stop = asyncio.Event()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        with suppress(NotImplementedError):  # pragma: no cover - platform without signal support
            loop.add_signal_handler(sig, stop.set)

    async def reservation_tick() -> None:
        await reap_reservations(
            factory,
            now=system_clock,
            new_movement_id=new_movement_id,
            batch_size=settings.reaper_batch_size,
        )

    async def idempotency_tick() -> None:
        await reap_idempotency_keys(
            factory,
            now=system_clock,
            ttl=timedelta(hours=settings.idempotency_ttl_hours),
            batch_size=settings.reaper_batch_size,
        )

    async def sweep_tick() -> None:
        await sweep_backorders(
            factory,
            now=system_clock,
            new_reservation_id=new_reservation_id,
            new_movement_id=new_movement_id,
            batch_size=settings.reaper_batch_size,
        )

    try:
        await asyncio.gather(
            run_forever(
                reservation_tick,
                interval=settings.reservation_reaper_interval_s,
                stop=stop,
            ),
            run_forever(
                idempotency_tick,
                interval=settings.idempotency_reaper_interval_s,
                stop=stop,
            ),
            run_forever(
                sweep_tick,
                interval=settings.backorder_sweep_interval_s,
                stop=stop,
            ),
        )
    finally:
        await engine.dispose()


if __name__ == "__main__":  # pragma: no cover - process entrypoint
    asyncio.run(run_workers())
