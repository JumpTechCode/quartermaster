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
    ArriveResponse,
    CancelResponse,
    CreatedLineOut,
    CreateOrderRequest,
    CreateOrderResponse,
    CreateReceiptRequest,
    CreateReceiptResponse,
    ExpectedLineOut,
    OrderLineView,
    OrderResponse,
    PackResponse,
    PickedLineOut,
    PickResponse,
    ReceiptLineView,
    ReceiptResponse,
    ReceiveLineOut,
    ReceiveRequest,
    ReceiveResponse,
    ShippedLineOut,
    ShipResponse,
)
from quartermaster.application.handlers.allocate import run_allocate
from quartermaster.application.handlers.arrive import run_arrive
from quartermaster.application.handlers.cancel import run_cancel
from quartermaster.application.handlers.create_order import run_create_order
from quartermaster.application.handlers.create_receipt import run_create_receipt
from quartermaster.application.handlers.pack import run_pack
from quartermaster.application.handlers.pick import run_pick
from quartermaster.application.handlers.receive import run_receive
from quartermaster.application.handlers.ship import run_ship
from quartermaster.application.queries import load_order, load_receipt
from quartermaster.domain.errors import OrderNotFound, ReceiptNotFound
from quartermaster.domain.ids import IdempotencyKey, LocationId, OrderId, ReceiptId, SkuId


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

    @router.post("/orders/{order_id}/pack", response_model=PackResponse)
    async def pack_route(
        order_id: UUID,
        idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    ) -> PackResponse:
        key = _require_key(idempotency_key)
        result = await run_pack(deps.uow_factory, OrderId(order_id), key)
        return PackResponse(order_id=result.order_id, state=result.state.value)

    @router.post("/orders/{order_id}/ship", response_model=ShipResponse)
    async def ship_route(
        order_id: UUID,
        idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    ) -> ShipResponse:
        key = _require_key(idempotency_key)
        result = await run_ship(deps.uow_factory, OrderId(order_id), key)
        return ShipResponse(
            order_id=result.order_id,
            state=result.state.value,
            lines=[
                ShippedLineOut(sku_id=line.sku_id, shipped=line.shipped) for line in result.lines
            ],
        )

    @router.post("/orders/{order_id}/cancel", response_model=CancelResponse)
    async def cancel_route(
        order_id: UUID,
        idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    ) -> CancelResponse:
        key = _require_key(idempotency_key)
        result = await run_cancel(
            deps.uow_factory,
            OrderId(order_id),
            key,
            now=deps.now,
            new_movement_id=deps.new_movement_id,
        )
        return CancelResponse(
            order_id=result.order_id,
            state=result.state.value,
            released_reservation_ids=list(result.released_reservation_ids),
        )

    @router.get("/receipts/{receipt_id}", response_model=ReceiptResponse)
    async def get_receipt(receipt_id: UUID) -> ReceiptResponse:
        view = await load_receipt(deps.uow_factory, ReceiptId(receipt_id))
        if view is None:
            raise ReceiptNotFound(f"receipt {receipt_id} does not exist")
        return ReceiptResponse(
            receipt_id=view.receipt_id,
            kind=view.kind.value,
            state=view.state.value,
            version=view.version,
            lines=[
                ReceiptLineView(sku_id=line.sku_id, expected=line.expected, received=line.received)
                for line in view.lines
            ],
        )

    @router.post(
        "/receipts", status_code=status.HTTP_201_CREATED, response_model=CreateReceiptResponse
    )
    async def create_receipt_route(
        body: CreateReceiptRequest,
        response: Response,
        idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    ) -> CreateReceiptResponse:
        key = _require_key(idempotency_key)
        lines = tuple((SkuId(line.sku_id), line.qty) for line in body.lines)
        result = await run_create_receipt(
            deps.uow_factory, lines, key, now=deps.now, new_receipt_id=deps.new_receipt_id
        )
        response.headers["Location"] = f"/receipts/{result.receipt_id}"
        return CreateReceiptResponse(
            receipt_id=result.receipt_id,
            kind=result.kind.value,
            state=result.state.value,
            lines=[
                ExpectedLineOut(sku_id=line.sku_id, expected=line.expected) for line in result.lines
            ],
        )

    @router.post("/receipts/{receipt_id}/arrive", response_model=ArriveResponse)
    async def arrive_route(
        receipt_id: UUID,
        idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    ) -> ArriveResponse:
        key = _require_key(idempotency_key)
        result = await run_arrive(deps.uow_factory, ReceiptId(receipt_id), key)
        return ArriveResponse(receipt_id=result.receipt_id, state=result.state.value)

    @router.post("/receipts/{receipt_id}/receive", response_model=ReceiveResponse)
    async def receive_route(
        receipt_id: UUID,
        body: ReceiveRequest,
        idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    ) -> ReceiveResponse:
        key = _require_key(idempotency_key)
        lines = tuple((SkuId(line.sku_id), line.qty) for line in body.lines)
        result = await run_receive(
            deps.uow_factory,
            ReceiptId(receipt_id),
            LocationId(body.location_id),
            lines,
            key,
            now=deps.now,
            new_movement_id=deps.new_movement_id,
        )
        return ReceiveResponse(
            receipt_id=result.receipt_id,
            state=result.state.value,
            lines=[
                ReceiveLineOut(sku_id=line.sku_id, received=line.received) for line in result.lines
            ],
        )

    return router
