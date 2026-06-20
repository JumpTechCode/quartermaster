"""HTTP tests for POST /orders/{id}/pack over fake deps."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

import httpx

from quartermaster.api.app import create_app
from quartermaster.domain.ids import OrderId
from quartermaster.domain.orders import Order
from quartermaster.domain.state_machines import OrderState
from tests.unit.api_helpers import make_deps
from tests.unit.fakes import FakeOrderRepo, FakeUnitOfWork

_OID = OrderId(UUID("00000000-0000-7000-8000-000000000001"))
_FIXED = datetime(2026, 6, 20, tzinfo=UTC)


def _client(uow: FakeUnitOfWork) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(make_deps(uow, order_id=_OID))),
        base_url="http://t",
    )


def _order(state: OrderState) -> Order:
    return Order(order_id=_OID, state=state, version=1, created_at=_FIXED)


async def _post(uow: FakeUnitOfWork, with_key: bool = True) -> httpx.Response:
    headers = {"Idempotency-Key": "k1"} if with_key else {}
    async with _client(uow) as client:
        return await client.post(f"/orders/{_OID}/pack", headers=headers)


async def test_pack_returns_200_packed() -> None:
    resp = await _post(FakeUnitOfWork(orders=FakeOrderRepo(order=_order(OrderState.PICKED))))
    assert resp.status_code == 200
    assert resp.json() == {"order_id": str(_OID), "state": "packed"}


async def test_pack_unknown_order_404() -> None:
    resp = await _post(FakeUnitOfWork(orders=FakeOrderRepo(order=None)))
    assert resp.status_code == 404
    assert resp.json()["error"] == "order_not_found"


async def test_pack_illegal_state_409() -> None:
    resp = await _post(FakeUnitOfWork(orders=FakeOrderRepo(order=_order(OrderState.ALLOCATED))))
    assert resp.status_code == 409
    assert resp.json()["error"] == "illegal_transition"


async def test_pack_missing_key_400() -> None:
    resp = await _post(
        FakeUnitOfWork(orders=FakeOrderRepo(order=_order(OrderState.PICKED))), with_key=False
    )
    assert resp.status_code == 400
    assert resp.json()["error"] == "missing_idempotency_key"
