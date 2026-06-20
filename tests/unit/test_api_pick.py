"""HTTP tests for POST /orders/{id}/pick over fake deps."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

import httpx

from quartermaster.api.app import create_app
from quartermaster.domain.ids import LocationId, OrderId, ReservationId, SkuId
from quartermaster.domain.orders import Order
from quartermaster.domain.reservations import Reservation
from quartermaster.domain.state_machines import OrderState, ReservationState
from tests.unit.api_helpers import make_deps
from tests.unit.fakes import FakeOrderRepo, FakeReservationRepo, FakeStockRepo, FakeUnitOfWork

_OID = OrderId(UUID("00000000-0000-7000-8000-000000000001"))
_FIXED = datetime(2026, 6, 19, tzinfo=UTC)


def _client(uow: FakeUnitOfWork) -> httpx.AsyncClient:
    app = create_app(make_deps(uow, order_id=_OID))
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t")


def _order(state: OrderState) -> Order:
    return Order(order_id=_OID, state=state, version=1, created_at=_FIXED)


def _held(qty: int) -> Reservation:
    return Reservation(
        reservation_id=ReservationId(UUID("00000000-0000-7000-8000-0000000000aa")),
        order_id=_OID,
        sku_id=SkuId("A"),
        location_id=LocationId("L1"),
        qty=qty,
        state=ReservationState.HELD,
        expires_at=_FIXED,
    )


async def _post(uow: FakeUnitOfWork, key: str = "k1", with_key: bool = True) -> httpx.Response:
    headers = {"Idempotency-Key": key} if with_key else {}
    async with _client(uow) as client:
        return await client.post(f"/orders/{_OID}/pick", headers=headers)


async def test_pick_returns_200_picked() -> None:
    uow = FakeUnitOfWork(
        stock=FakeStockRepo(),
        orders=FakeOrderRepo(order=_order(OrderState.ALLOCATED)),
        reservations=FakeReservationRepo([_held(5)]),
    )
    resp = await _post(uow)
    assert resp.status_code == 200
    body = resp.json()
    assert body["state"] == "picked"
    assert body["lines"] == [{"sku_id": "A", "picked": 5}]


async def test_pick_unknown_order_404() -> None:
    uow = FakeUnitOfWork(orders=FakeOrderRepo(order=None))
    resp = await _post(uow)
    assert resp.status_code == 404
    assert resp.json()["error"] == "order_not_found"


async def test_pick_illegal_state_409() -> None:
    uow = FakeUnitOfWork(
        orders=FakeOrderRepo(order=_order(OrderState.CREATED)),
        reservations=FakeReservationRepo([_held(5)]),
    )
    resp = await _post(uow)
    assert resp.status_code == 409
    assert resp.json()["error"] == "illegal_transition"


async def test_pick_missing_key_400() -> None:
    uow = FakeUnitOfWork(
        orders=FakeOrderRepo(order=_order(OrderState.ALLOCATED)),
        reservations=FakeReservationRepo([_held(5)]),
    )
    resp = await _post(uow, with_key=False)
    assert resp.status_code == 400
    assert resp.json()["error"] == "missing_idempotency_key"
