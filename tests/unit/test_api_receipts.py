"""HTTP tests for the receipts endpoints over fake deps."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

import httpx

from quartermaster.api.app import create_app
from quartermaster.domain.ids import LocationId, ReceiptId, SkuId
from quartermaster.domain.receipts import Receipt, ReceiptKind, ReceiptLine
from quartermaster.domain.state_machines import ReceiptState
from tests.unit.api_helpers import make_deps
from tests.unit.fakes import (
    FakeCatalogRepo,
    FakeIdempotencyRepo,
    FakeMovementRepo,
    FakeReceiptRepo,
    FakeStockRepo,
    FakeUnitOfWork,
)

_RCID = UUID("00000000-0000-7000-8000-000000000004")
_FIXED = datetime(2026, 6, 20, tzinfo=UTC)


def _client(uow: FakeUnitOfWork) -> httpx.AsyncClient:
    app = create_app(make_deps(uow, receipt_id=ReceiptId(_RCID)))
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t")


def _arrived_receipt() -> Receipt:
    return Receipt(
        ReceiptId(_RCID), ReceiptKind.SUPPLIER_RECEIPT, ReceiptState.ARRIVED, 2, _FIXED, None
    )


async def test_create_receipt_201_with_location() -> None:
    receipts = FakeReceiptRepo()
    uow = FakeUnitOfWork(
        receipts=receipts,
        idempotency=FakeIdempotencyRepo(),
        catalog=FakeCatalogRepo(known={SkuId("A")}),
    )
    async with _client(uow) as client:
        resp = await client.post(
            "/receipts",
            json={"lines": [{"sku_id": "A", "qty": 5}]},
            headers={"Idempotency-Key": "k1"},
        )
    assert resp.status_code == 201
    assert resp.headers["Location"] == f"/receipts/{_RCID}"
    body = resp.json()
    assert body["state"] == "expected"
    assert body["kind"] == "supplier_receipt"
    assert body["lines"] == [{"sku_id": "A", "expected": 5}]
    assert len(receipts.inserted) == 1


async def test_arrive_returns_arrived() -> None:
    receipt = Receipt(
        ReceiptId(_RCID), ReceiptKind.SUPPLIER_RECEIPT, ReceiptState.EXPECTED, 1, _FIXED, None
    )
    uow = FakeUnitOfWork(
        receipts=FakeReceiptRepo(receipt=receipt), idempotency=FakeIdempotencyRepo()
    )
    async with _client(uow) as client:
        resp = await client.post(f"/receipts/{_RCID}/arrive", headers={"Idempotency-Key": "k1"})
    assert resp.status_code == 200
    assert resp.json()["state"] == "arrived"


async def test_receive_lands_and_returns_received() -> None:
    line = ReceiptLine(ReceiptId(_RCID), SkuId("A"), 5, 0)
    uow = FakeUnitOfWork(
        receipts=FakeReceiptRepo(receipt=_arrived_receipt(), lines=[line]),
        stock=FakeStockRepo(),
        movements=FakeMovementRepo(),
        catalog=FakeCatalogRepo(known_locations={LocationId("RCV")}),
        idempotency=FakeIdempotencyRepo(),
    )
    async with _client(uow) as client:
        resp = await client.post(
            f"/receipts/{_RCID}/receive",
            json={"location_id": "RCV", "lines": [{"sku_id": "A", "qty": 5}]},
            headers={"Idempotency-Key": "k1"},
        )
    assert resp.status_code == 200
    body = resp.json()
    assert body["state"] == "received"
    assert body["lines"] == [{"sku_id": "A", "received": 5}]


async def test_receive_unknown_location_422() -> None:
    line = ReceiptLine(ReceiptId(_RCID), SkuId("A"), 5, 0)
    uow = FakeUnitOfWork(
        receipts=FakeReceiptRepo(receipt=_arrived_receipt(), lines=[line]),
        stock=FakeStockRepo(),
        movements=FakeMovementRepo(),
        catalog=FakeCatalogRepo(known_locations=set()),
        idempotency=FakeIdempotencyRepo(),
    )
    async with _client(uow) as client:
        resp = await client.post(
            f"/receipts/{_RCID}/receive",
            json={"location_id": "NOPE", "lines": [{"sku_id": "A", "qty": 5}]},
            headers={"Idempotency-Key": "k1"},
        )
    assert resp.status_code == 422
    assert resp.json()["error"] == "unknown_location"


async def test_get_receipt_404_when_missing() -> None:
    uow = FakeUnitOfWork(receipts=FakeReceiptRepo(receipt=None))
    async with _client(uow) as client:
        resp = await client.get(f"/receipts/{_RCID}")
    assert resp.status_code == 404
    assert resp.json()["error"] == "receipt_not_found"
