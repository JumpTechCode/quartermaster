"""Composition root.

The only module permitted to import concrete adapters and wire them to the
application ports. ``build_app`` reads settings, builds the async engine and the
seam bundle, and returns the wired FastAPI app; serve it with
``uvicorn quartermaster.app:build_app --factory``. Building lazily (a factory,
not a module-level app) keeps importing this module side-effect-free, so the
boundary checker and tests can import it without a configured database.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

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
