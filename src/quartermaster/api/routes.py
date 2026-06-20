"""HTTP routes: thin adapters that parse input, call an application runner, and
return a response model. The router closes over the injected ``Deps``.
"""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter

from quartermaster.api.deps import Deps
from quartermaster.api.errors import MissingIdempotencyKey
from quartermaster.api.schemas import OrderLineView, OrderResponse
from quartermaster.application.queries import load_order
from quartermaster.domain.errors import OrderNotFound
from quartermaster.domain.ids import IdempotencyKey, OrderId


def _require_key(idempotency_key: str | None) -> IdempotencyKey:
    if not idempotency_key:
        raise MissingIdempotencyKey("the Idempotency-Key header is required")
    return IdempotencyKey(idempotency_key)


def build_router(deps: Deps) -> APIRouter:
    router = APIRouter()

    @router.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @router.get("/orders/{order_id}", response_model=OrderResponse)
    async def get_order(order_id: UUID) -> OrderResponse:
        view = await load_order(deps.uow_factory, OrderId(order_id))
        if view is None:
            raise OrderNotFound(f"order {order_id} does not exist")
        return OrderResponse(
            order_id=view.order_id,
            state=view.state.value,
            version=view.version,
            lines=[
                OrderLineView(
                    sku_id=line.sku_id,
                    ordered=line.ordered,
                    allocated=line.allocated,
                    picked=line.picked,
                    shipped=line.shipped,
                )
                for line in view.lines
            ],
        )

    return router
