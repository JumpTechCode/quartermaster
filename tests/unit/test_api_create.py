"""HTTP tests for POST /orders over fake deps."""

from __future__ import annotations

from uuid import UUID

import httpx

from quartermaster.api.app import create_app
from quartermaster.application.commands import CreateOrderCommand
from quartermaster.application.ports import ClaimOutcome, StoredResponse
from quartermaster.domain.idempotency import IdempotencyStatus
from quartermaster.domain.ids import IdempotencyKey, SkuId
from tests.unit.api_helpers import make_deps
from tests.unit.fakes import FakeCatalogRepo, FakeIdempotencyRepo, FakeOrderRepo, FakeUnitOfWork

_OID = UUID("00000000-0000-7000-8000-000000000001")


def _client(uow: FakeUnitOfWork) -> httpx.AsyncClient:
    from quartermaster.domain.ids import OrderId

    app = create_app(make_deps(uow, order_id=OrderId(_OID)))
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t")


def _uow(
    orders: FakeOrderRepo | None = None,
    idempotency: FakeIdempotencyRepo | None = None,
    catalog: FakeCatalogRepo | None = None,
) -> FakeUnitOfWork:
    return FakeUnitOfWork(
        orders=orders or FakeOrderRepo(),
        idempotency=idempotency or FakeIdempotencyRepo(),
        catalog=catalog or FakeCatalogRepo(known={SkuId("A"), SkuId("B")}),
    )


async def test_create_returns_201_with_location() -> None:
    orders = FakeOrderRepo()
    uow = _uow(orders=orders)
    async with _client(uow) as client:
        resp = await client.post(
            "/orders",
            json={"lines": [{"sku_id": "A", "qty": 5}]},
            headers={"Idempotency-Key": "k1"},
        )
    assert resp.status_code == 201
    assert resp.headers["Location"] == f"/orders/{_OID}"
    body = resp.json()
    assert body["order_id"] == str(_OID)
    assert body["state"] == "created"
    assert body["lines"] == [{"sku_id": "A", "ordered": 5}]
    assert len(orders.inserted) == 1


async def test_create_missing_key_400() -> None:
    async with _client(_uow()) as client:
        resp = await client.post("/orders", json={"lines": [{"sku_id": "A", "qty": 5}]})
    assert resp.status_code == 400
    assert resp.json()["error"] == "missing_idempotency_key"


async def test_create_validation_422() -> None:
    async with _client(_uow()) as client:
        resp = await client.post("/orders", json={"lines": []}, headers={"Idempotency-Key": "k1"})
    assert resp.status_code == 422
    assert resp.json()["error"] == "validation_error"
    # Detail is a shaped field:message summary, not the raw verbose dump.
    detail = resp.json()["detail"]
    assert "lines" in detail
    assert "loc=" not in detail
    assert "url=" not in detail


async def test_create_unknown_sku_422() -> None:
    uow = _uow(catalog=FakeCatalogRepo(known={SkuId("A")}))
    async with _client(uow) as client:
        resp = await client.post(
            "/orders",
            json={"lines": [{"sku_id": "A", "qty": 1}, {"sku_id": "B", "qty": 1}]},
            headers={"Idempotency-Key": "k1"},
        )
    assert resp.status_code == 422
    assert resp.json()["error"] == "unknown_sku"


async def test_create_replay_returns_stored_without_reinserting() -> None:
    stored = StoredResponse(
        command_fingerprint=CreateOrderCommand(
            ((SkuId("A"), 5),), IdempotencyKey("k1")
        ).fingerprint(),
        status=IdempotencyStatus.SUCCEEDED,
        response={
            "order_id": str(_OID),
            "state": "created",
            "lines": [{"sku_id": "A", "ordered": 5}],
        },
    )
    orders = FakeOrderRepo()
    uow = _uow(
        orders=orders,
        idempotency=FakeIdempotencyRepo(claim_outcome=ClaimOutcome.EXISTS, stored=stored),
    )
    async with _client(uow) as client:
        resp = await client.post(
            "/orders",
            json={"lines": [{"sku_id": "A", "qty": 5}]},
            headers={"Idempotency-Key": "k1"},
        )
    assert resp.status_code == 201
    assert resp.json()["order_id"] == str(_OID)
    assert orders.inserted == []  # replay: the handler never ran
