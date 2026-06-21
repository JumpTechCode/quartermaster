"""HTTP tests for the returns endpoint over fake deps."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

import httpx

from quartermaster.api.app import create_app
from quartermaster.domain.ids import OrderId, ReceiptId, SkuId
from quartermaster.domain.orders import Order, OrderLine
from quartermaster.domain.receipts import Receipt, ReceiptKind, ReceiptLine
from quartermaster.domain.state_machines import OrderState, ReceiptState
from tests.unit.api_helpers import make_deps
from tests.unit.fakes import (
    FakeIdempotencyRepo,
    FakeOrderRepo,
    FakeReceiptRepo,
    FakeUnitOfWork,
)

_OID = UUID("00000000-0000-7000-8000-000000000001")
_RCID = UUID("00000000-0000-7000-8000-000000000004")
_FIXED = datetime(2026, 6, 20, tzinfo=UTC)


def _client(uow: FakeUnitOfWork) -> httpx.AsyncClient:
    app = create_app(make_deps(uow, receipt_id=ReceiptId(_RCID)))
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t")


def _shipped_uow(receipts: FakeReceiptRepo | None = None) -> FakeUnitOfWork:
    order = Order(order_id=OrderId(_OID), state=OrderState.SHIPPED, version=6, created_at=_FIXED)
    line = OrderLine(
        order_id=OrderId(_OID), sku_id=SkuId("A"), ordered=5, allocated=5, picked=5, shipped=5
    )
    return FakeUnitOfWork(
        orders=FakeOrderRepo(order=order, lines=[line]),
        receipts=receipts or FakeReceiptRepo(),
        idempotency=FakeIdempotencyRepo(),
    )


async def test_create_return_201_with_location() -> None:
    receipts = FakeReceiptRepo()
    async with _client(_shipped_uow(receipts)) as client:
        resp = await client.post(
            "/returns",
            json={"order_id": str(_OID), "lines": [{"sku_id": "A", "qty": 3}]},
            headers={"Idempotency-Key": "k1"},
        )
    assert resp.status_code == 201
    assert resp.headers["Location"] == f"/receipts/{_RCID}"
    body = resp.json()
    assert body["kind"] == "customer_rma"
    assert body["state"] == "expected"
    assert body["lines"] == [{"sku_id": "A", "expected": 3}]
    assert len(receipts.inserted) == 1


async def test_create_return_unknown_order_404() -> None:
    uow = FakeUnitOfWork(
        orders=FakeOrderRepo(order=None),
        receipts=FakeReceiptRepo(),
        idempotency=FakeIdempotencyRepo(),
    )
    async with _client(uow) as client:
        resp = await client.post(
            "/returns",
            json={"order_id": str(_OID), "lines": [{"sku_id": "A", "qty": 1}]},
            headers={"Idempotency-Key": "k1"},
        )
    assert resp.status_code == 404
    assert resp.json()["error"] == "order_not_found"


async def test_create_return_over_shipped_422() -> None:
    async with _client(_shipped_uow()) as client:
        resp = await client.post(
            "/returns",
            json={"order_id": str(_OID), "lines": [{"sku_id": "A", "qty": 9}]},
            headers={"Idempotency-Key": "k1"},
        )
    assert resp.status_code == 422
    assert resp.json()["error"] == "return_not_allowed"


async def test_create_return_duplicate_sku_422_validation() -> None:
    async with _client(_shipped_uow()) as client:
        resp = await client.post(
            "/returns",
            json={
                "order_id": str(_OID),
                "lines": [{"sku_id": "A", "qty": 1}, {"sku_id": "A", "qty": 2}],
            },
            headers={"Idempotency-Key": "k1"},
        )
    assert resp.status_code == 422
    assert resp.json()["error"] == "validation_error"


async def test_create_return_missing_idempotency_key_400() -> None:
    async with _client(_shipped_uow()) as client:
        resp = await client.post(
            "/returns",
            json={"order_id": str(_OID), "lines": [{"sku_id": "A", "qty": 1}]},
        )
    assert resp.status_code == 400
    assert resp.json()["error"] == "missing_idempotency_key"


async def test_get_receipt_includes_origin_order_id() -> None:
    rec = Receipt(
        ReceiptId(_RCID), ReceiptKind.CUSTOMER_RMA, ReceiptState.EXPECTED, 1, _FIXED, OrderId(_OID)
    )
    line = ReceiptLine(ReceiptId(_RCID), SkuId("A"), 3, 0)
    uow = FakeUnitOfWork(receipts=FakeReceiptRepo(receipt=rec, lines=[line]))
    async with _client(uow) as client:
        resp = await client.get(f"/receipts/{_RCID}")
    assert resp.status_code == 200
    body = resp.json()
    assert body["kind"] == "customer_rma"
    assert body["origin_order_id"] == str(_OID)


async def test_get_supplier_receipt_origin_is_null() -> None:
    rec = Receipt(
        ReceiptId(_RCID),
        ReceiptKind.SUPPLIER_RECEIPT,
        ReceiptState.EXPECTED,
        1,
        _FIXED,
        None,
    )
    uow = FakeUnitOfWork(receipts=FakeReceiptRepo(receipt=rec, lines=[]))
    async with _client(uow) as client:
        resp = await client.get(f"/receipts/{_RCID}")
    assert resp.status_code == 200
    assert resp.json()["origin_order_id"] is None
