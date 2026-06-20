"""Unit tests for the create_receipt handler (no DB)."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

import pytest

from quartermaster.application.commands import CreateReceiptCommand
from quartermaster.application.handlers.create_receipt import create_receipt
from quartermaster.domain.errors import UnknownSku
from quartermaster.domain.ids import IdempotencyKey, ReceiptId, SkuId
from quartermaster.domain.receipts import ReceiptKind
from quartermaster.domain.state_machines import ReceiptState
from tests.unit.fakes import FakeCatalogRepo, FakeReceiptRepo, FakeUnitOfWork

RID = ReceiptId(UUID("00000000-0000-7000-8000-000000000005"))
KEY = IdempotencyKey("k")


def _now() -> datetime:
    return datetime(2026, 6, 20, 12, 0, tzinfo=UTC)


async def test_create_receipt_inserts_header_and_lines() -> None:
    receipts = FakeReceiptRepo()
    catalog = FakeCatalogRepo(known={SkuId("A"), SkuId("B")})
    uow = FakeUnitOfWork(receipts=receipts, catalog=catalog)

    result = await create_receipt(
        uow,
        CreateReceiptCommand(((SkuId("A"), 5), (SkuId("B"), 3)), KEY),
        now=_now,
        new_receipt_id=lambda: RID,
    )

    assert result.receipt_id == RID
    assert result.kind is ReceiptKind.SUPPLIER_RECEIPT
    assert result.state is ReceiptState.EXPECTED
    assert [(line.sku_id, line.expected) for line in result.lines] == [("A", 5), ("B", 3)]
    assert len(receipts.inserted) == 1
    header, lines = receipts.inserted[0]
    assert header.state is ReceiptState.EXPECTED
    assert header.kind is ReceiptKind.SUPPLIER_RECEIPT
    assert header.origin_order_id is None
    assert [(line.sku_id, line.expected, line.received) for line in lines] == [
        ("A", 5, 0),
        ("B", 3, 0),
    ]


async def test_create_receipt_unknown_sku_rejected() -> None:
    uow = FakeUnitOfWork(receipts=FakeReceiptRepo(), catalog=FakeCatalogRepo(known={SkuId("A")}))
    with pytest.raises(UnknownSku):
        await create_receipt(
            uow,
            CreateReceiptCommand(((SkuId("A"), 1), (SkuId("B"), 1)), KEY),
            now=_now,
            new_receipt_id=lambda: RID,
        )
