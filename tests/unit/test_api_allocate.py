"""HTTP tests for POST /orders/{id}/allocate over fake deps."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

import httpx

from quartermaster.api.app import create_app
from quartermaster.application.ports import ClaimOutcome, StoredResponse
from quartermaster.domain.idempotency import IdempotencyStatus
from quartermaster.domain.ids import LocationId, OrderId, SkuId
from quartermaster.domain.orders import Order, OrderLine
from quartermaster.domain.state_machines import OrderState
from tests.unit.api_helpers import make_deps
from tests.unit.fakes import FakeIdempotencyRepo, FakeOrderRepo, FakeStockRepo, FakeUnitOfWork

_OID = OrderId(UUID("00000000-0000-7000-8000-000000000001"))
_RID = "00000000-0000-7000-8000-000000000002"
_FIXED = datetime(2026, 6, 18, tzinfo=UTC)


def _client(uow: FakeUnitOfWork) -> httpx.AsyncClient:
    app = create_app(make_deps(uow, order_id=_OID))
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t")


def _order(state: OrderState) -> Order:
    return Order(order_id=_OID, state=state, version=1, created_at=_FIXED)


def _line(ordered: int) -> OrderLine:
    return OrderLine(
        order_id=_OID, sku_id=SkuId("A"), ordered=ordered, allocated=0, picked=0, shipped=0
    )


async def _post(uow: FakeUnitOfWork, key: str = "k1") -> httpx.Response:
    async with _client(uow) as client:
        return await client.post(f"/orders/{_OID}/allocate", headers={"Idempotency-Key": key})


async def test_allocate_full_returns_200_allocated() -> None:
    uow = FakeUnitOfWork(
        stock=FakeStockRepo({(SkuId("A"), LocationId("L1")): 5}),
        orders=FakeOrderRepo(order=_order(OrderState.CREATED), lines=[_line(5)]),
    )
    resp = await _post(uow)
    assert resp.status_code == 200
    body = resp.json()
    assert body["state"] == "allocated"
    assert body["lines"] == [{"sku_id": "A", "allocated": 5}]
    assert body["reservation_ids"] == [_RID]


async def test_allocate_short_returns_200_backordered() -> None:
    uow = FakeUnitOfWork(
        stock=FakeStockRepo({(SkuId("A"), LocationId("L1")): 2}),
        orders=FakeOrderRepo(order=_order(OrderState.CREATED), lines=[_line(5)]),
    )
    resp = await _post(uow)
    assert resp.status_code == 200
    assert resp.json()["state"] == "backordered"


async def test_allocate_unknown_order_404() -> None:
    uow = FakeUnitOfWork(orders=FakeOrderRepo(order=None))
    resp = await _post(uow)
    assert resp.status_code == 404
    assert resp.json()["error"] == "order_not_found"


async def test_allocate_illegal_state_409() -> None:
    uow = FakeUnitOfWork(
        stock=FakeStockRepo({(SkuId("A"), LocationId("L1")): 5}),
        orders=FakeOrderRepo(order=_order(OrderState.SHIPPED), lines=[_line(5)]),
    )
    resp = await _post(uow)
    assert resp.status_code == 409
    assert resp.json()["error"] == "illegal_transition"


async def test_allocate_key_reuse_409() -> None:
    stored = StoredResponse(
        command_fingerprint="some-other-fingerprint",
        status=IdempotencyStatus.SUCCEEDED,
        response={"order_id": str(_OID), "state": "allocated", "lines": [], "reservation_ids": []},
    )
    uow = FakeUnitOfWork(
        orders=FakeOrderRepo(order=_order(OrderState.CREATED), lines=[_line(5)]),
        idempotency=FakeIdempotencyRepo(claim_outcome=ClaimOutcome.EXISTS, stored=stored),
    )
    resp = await _post(uow)
    assert resp.status_code == 409
    assert resp.json()["error"] == "idempotency_key_reuse"
    # Detail is a sentence referencing the key, not a bare key token.
    detail = resp.json()["detail"]
    assert "k1" in detail
    assert "reused" in detail


async def test_allocate_retry_exhausted_503() -> None:
    uow = FakeUnitOfWork(
        stock=FakeStockRepo(),  # no stock -> backorder target, but cas always fails
        orders=FakeOrderRepo(order=_order(OrderState.CREATED), lines=[_line(5)], cas_result=False),
    )
    resp = await _post(uow)
    assert resp.status_code == 503
    assert resp.json()["error"] == "retry_exhausted"
    assert resp.headers["Retry-After"] == "0"
