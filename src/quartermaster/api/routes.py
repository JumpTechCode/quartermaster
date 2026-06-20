"""HTTP routes: thin adapters that parse input, call an application runner, and
return a response model. The router closes over the injected ``Deps``.
"""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Header, Response, status

from quartermaster.api.deps import Deps
from quartermaster.api.errors import MissingIdempotencyKey
from quartermaster.api.schemas import (
    AllocateResponse,
    AllocationLineOut,
    CreatedLineOut,
    CreateOrderRequest,
    CreateOrderResponse,
    OrderLineView,
    OrderResponse,
    PickedLineOut,
    PickResponse,
)
from quartermaster.application.handlers.allocate import run_allocate
from quartermaster.application.handlers.create_order import run_create_order
from quartermaster.application.handlers.pick import run_pick
from quartermaster.application.queries import load_order
from quartermaster.domain.errors import OrderNotFound
from quartermaster.domain.ids import IdempotencyKey, OrderId, SkuId


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

    @router.post("/orders", status_code=status.HTTP_201_CREATED, response_model=CreateOrderResponse)
    async def create_order_route(
        body: CreateOrderRequest,
        response: Response,
        idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    ) -> CreateOrderResponse:
        key = _require_key(idempotency_key)
        lines = tuple((SkuId(line.sku_id), line.qty) for line in body.lines)
        result = await run_create_order(
            deps.uow_factory, lines, key, now=deps.now, new_order_id=deps.new_order_id
        )
        response.headers["Location"] = f"/orders/{result.order_id}"
        return CreateOrderResponse(
            order_id=result.order_id,
            state=result.state.value,
            lines=[
                CreatedLineOut(sku_id=line.sku_id, ordered=line.ordered) for line in result.lines
            ],
        )

    @router.post("/orders/{order_id}/allocate", response_model=AllocateResponse)
    async def allocate_route(
        order_id: UUID,
        idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    ) -> AllocateResponse:
        key = _require_key(idempotency_key)
        result = await run_allocate(
            deps.uow_factory,
            OrderId(order_id),
            key,
            now=deps.now,
            new_reservation_id=deps.new_reservation_id,
            new_movement_id=deps.new_movement_id,
        )
        return AllocateResponse(
            order_id=result.order_id,
            state=result.state.value,
            lines=[
                AllocationLineOut(sku_id=line.sku_id, allocated=line.allocated)
                for line in result.lines
            ],
            reservation_ids=list(result.reservation_ids),
        )

    @router.post("/orders/{order_id}/pick", response_model=PickResponse)
    async def pick_route(
        order_id: UUID,
        idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    ) -> PickResponse:
        key = _require_key(idempotency_key)
        result = await run_pick(
            deps.uow_factory,
            OrderId(order_id),
            key,
            now=deps.now,
            new_movement_id=deps.new_movement_id,
        )
        return PickResponse(
            order_id=result.order_id,
            state=result.state.value,
            lines=[PickedLineOut(sku_id=line.sku_id, picked=line.picked) for line in result.lines],
        )

    return router
