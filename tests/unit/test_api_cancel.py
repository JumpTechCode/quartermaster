"""HTTP tests for POST /orders/{id}/cancel over fake deps."""

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
from tests.unit.fakes import FakeOrderRepo, FakeReservationRepo, FakeUnitOfWork

_OID = OrderId(UUID("00000000-0000-7000-8000-000000000001"))
_FIXED = datetime(2026, 6, 20, tzinfo=UTC)


def _client(uow: FakeUnitOfWork) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(make_deps(uow, order_id=_OID))),
        base_url="http://t",
    )


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


async def _post(uow: FakeUnitOfWork, with_key: bool = True) -> httpx.Response:
    headers = {"Idempotency-Key": "k1"} if with_key else {}
    async with _client(uow) as client:
        return await client.post(f"/orders/{_OID}/cancel", headers=headers)


async def test_cancel_returns_200_cancelled() -> None:
    uow = FakeUnitOfWork(
        orders=FakeOrderRepo(order=_order(OrderState.ALLOCATED)),
        reservations=FakeReservationRepo([_held(5)]),
    )
    resp = await _post(uow)
    assert resp.status_code == 200
    body = resp.json()
    assert body["state"] == "cancelled"
    assert body["released_reservation_ids"] == ["00000000-0000-7000-8000-0000000000aa"]


async def test_cancel_unknown_order_404() -> None:
    resp = await _post(FakeUnitOfWork(orders=FakeOrderRepo(order=None)))
    assert resp.status_code == 404
    assert resp.json()["error"] == "order_not_found"


async def test_cancel_illegal_state_409() -> None:
    resp = await _post(
        FakeUnitOfWork(
            orders=FakeOrderRepo(order=_order(OrderState.PICKING)),
            reservations=FakeReservationRepo([_held(5)]),
        )
    )
    assert resp.status_code == 409
    assert resp.json()["error"] == "illegal_transition"


async def test_cancel_missing_key_400() -> None:
    resp = await _post(
        FakeUnitOfWork(
            orders=FakeOrderRepo(order=_order(OrderState.ALLOCATED)),
            reservations=FakeReservationRepo([_held(5)]),
        ),
        with_key=False,
    )
    assert resp.status_code == 400
    assert resp.json()["error"] == "missing_idempotency_key"
