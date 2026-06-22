"""HTTP tests for /healthz and GET /orders/{id} over fake deps (no Postgres)."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

import httpx

from quartermaster.api.app import create_app
from quartermaster.domain.ids import OrderId, SkuId
from quartermaster.domain.orders import Order, OrderLine
from quartermaster.domain.state_machines import OrderState
from tests.unit.api_helpers import make_deps
from tests.unit.fakes import FakeOrderRepo, FakeUnitOfWork

_OID = OrderId(UUID("00000000-0000-7000-8000-000000000001"))


def _client(uow: FakeUnitOfWork) -> httpx.AsyncClient:
    app = create_app(make_deps(uow, order_id=_OID))
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t")


async def test_healthz() -> None:
    async with _client(FakeUnitOfWork()) as client:
        resp = await client.get("/healthz")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


async def test_get_order_found() -> None:
    order = Order(
        order_id=_OID,
        state=OrderState.CREATED,
        version=1,
        created_at=datetime(2026, 6, 18, tzinfo=UTC),
    )
    line = OrderLine(order_id=_OID, sku_id=SkuId("A"), ordered=5, allocated=0, picked=0, shipped=0)
    uow = FakeUnitOfWork(orders=FakeOrderRepo(order=order, lines=[line]))
    async with _client(uow) as client:
        resp = await client.get(f"/orders/{_OID}")
    assert resp.status_code == 200
    body = resp.json()
    assert body["state"] == "created" and body["version"] == 1
    assert body["lines"] == [
        {"sku_id": "A", "ordered": 5, "allocated": 0, "picked": 0, "shipped": 0}
    ]


async def test_get_order_missing_404() -> None:
    uow = FakeUnitOfWork(orders=FakeOrderRepo(order=None))
    async with _client(uow) as client:
        resp = await client.get(f"/orders/{_OID}")
    assert resp.status_code == 404
    assert resp.json()["error"] == "order_not_found"


async def test_get_order_reads_through_the_read_factory() -> None:
    """GET routes use deps.read_uow_factory (REPEATABLE READ), not the write factory."""
    order = Order(
        order_id=_OID,
        state=OrderState.CREATED,
        version=1,
        created_at=datetime(2026, 6, 18, tzinfo=UTC),
    )
    read_uow = FakeUnitOfWork(orders=FakeOrderRepo(order=order, lines=[]))
    write_uow = FakeUnitOfWork(orders=FakeOrderRepo(order=None))  # a 404 if reads hit it
    deps = make_deps(write_uow, order_id=_OID, read_uow=read_uow)
    app = create_app(deps)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://t"
    ) as client:
        resp = await client.get(f"/orders/{_OID}")
    assert resp.status_code == 200
    assert resp.json()["version"] == 1
