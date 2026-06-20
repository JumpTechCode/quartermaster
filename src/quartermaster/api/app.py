"""The FastAPI app factory. The only public entry point of the api/ package.

``create_app`` takes a fully-assembled ``Deps`` and returns a wired app; it
imports nothing from ``adapters``. The composition root passes the concrete
seams and an optional lifespan.
"""

from __future__ import annotations

from collections.abc import Callable
from contextlib import AbstractAsyncContextManager

from fastapi import FastAPI

from quartermaster.api.deps import Deps
from quartermaster.api.errors import register_error_handlers
from quartermaster.api.routes import build_router

Lifespan = Callable[[FastAPI], AbstractAsyncContextManager[None]]


def create_app(deps: Deps, *, lifespan: Lifespan | None = None) -> FastAPI:
    """Build the FastAPI app over the injected ``deps``."""
    app = FastAPI(title="Quartermaster", lifespan=lifespan)
    register_error_handlers(app)
    app.include_router(build_router(deps))
    return app
