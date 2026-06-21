"""Unit tests for the create_return handler (no DB)."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

import pytest

from quartermaster.application.commands import CreateReturnCommand
from quartermaster.application.handlers.create_return import create_return
from quartermaster.domain.errors import OrderNotFound, ReturnNotAllowed
from quartermaster.domain.ids import IdempotencyKey, OrderId, ReceiptId, SkuId
from quartermaster.domain.orders import Order, OrderLine
from quartermaster.domain.receipts import ReceiptKind
from quartermaster.domain.state_machines import OrderState, ReceiptState
from tests.unit.fakes import FakeOrderRepo, FakeReceiptRepo, FakeUnitOfWork

OID = OrderId(UUID("00000000-0000-7000-8000-000000000001"))
RID = ReceiptId(UUID("00000000-0000-7000-8000-000000000005"))
KEY = IdempotencyKey("k")
_FIXED = datetime(2026, 6, 20, 12, 0, tzinfo=UTC)


def _now() -> datetime:
    return _FIXED


def _shipped_order(**shipped: int) -> tuple[Order, list[OrderLine]]:
    """A SHIPPED order whose given SKUs were fully ordered/allocated/picked/shipped."""
    order = Order(order_id=OID, state=OrderState.SHIPPED, version=6, created_at=_FIXED)
    lines = [
        OrderLine(
            order_id=OID, sku_id=SkuId(sku), ordered=qty, allocated=qty, picked=qty, shipped=qty
        )
        for sku, qty in shipped.items()
    ]
    return order, lines


def _uow(order: Order | None, lines: list[OrderLine], receipts: FakeReceiptRepo) -> FakeUnitOfWork:
    return FakeUnitOfWork(orders=FakeOrderRepo(order=order, lines=lines), receipts=receipts)


async def test_create_return_mints_rma_referencing_order() -> None:
    order, lines = _shipped_order(A=5, B=3)
    receipts = FakeReceiptRepo()
    result = await create_return(
        _uow(order, lines, receipts),
        CreateReturnCommand(OID, ((SkuId("A"), 5), (SkuId("B"), 2)), KEY),
        now=_now,
        new_receipt_id=lambda: RID,
    )

    assert result.receipt_id == RID
    assert result.kind is ReceiptKind.CUSTOMER_RMA
    assert result.state is ReceiptState.EXPECTED
    assert [(line.sku_id, line.expected) for line in result.lines] == [("A", 5), ("B", 2)]
    assert len(receipts.inserted) == 1
    header, inserted = receipts.inserted[0]
    assert header.kind is ReceiptKind.CUSTOMER_RMA
    assert header.origin_order_id == OID
    assert header.state is ReceiptState.EXPECTED
    assert [(line.sku_id, line.expected, line.received) for line in inserted] == [
        ("A", 5, 0),
        ("B", 2, 0),
    ]


async def test_create_return_partial_under_shipped_allowed() -> None:
    order, lines = _shipped_order(A=5)
    result = await create_return(
        _uow(order, lines, FakeReceiptRepo()),
        CreateReturnCommand(OID, ((SkuId("A"), 2),), KEY),
        now=_now,
        new_receipt_id=lambda: RID,
    )
    assert [(line.sku_id, line.expected) for line in result.lines] == [("A", 2)]


async def test_create_return_unknown_order_rejected() -> None:
    with pytest.raises(OrderNotFound):
        await create_return(
            _uow(None, [], FakeReceiptRepo()),
            CreateReturnCommand(OID, ((SkuId("A"), 1),), KEY),
            now=_now,
            new_receipt_id=lambda: RID,
        )


async def test_create_return_unshipped_order_rejected() -> None:
    order = Order(order_id=OID, state=OrderState.ALLOCATED, version=2, created_at=_FIXED)
    line = OrderLine(order_id=OID, sku_id=SkuId("A"), ordered=5, allocated=5, picked=0, shipped=0)
    with pytest.raises(ReturnNotAllowed):
        await create_return(
            _uow(order, [line], FakeReceiptRepo()),
            CreateReturnCommand(OID, ((SkuId("A"), 1),), KEY),
            now=_now,
            new_receipt_id=lambda: RID,
        )


async def test_create_return_sku_not_on_order_rejected() -> None:
    order, lines = _shipped_order(A=5)
    with pytest.raises(ReturnNotAllowed):
        await create_return(
            _uow(order, lines, FakeReceiptRepo()),
            CreateReturnCommand(OID, ((SkuId("B"), 1),), KEY),
            now=_now,
            new_receipt_id=lambda: RID,
        )


async def test_create_return_sku_shipped_zero_rejected() -> None:
    order = Order(order_id=OID, state=OrderState.SHIPPED, version=6, created_at=_FIXED)
    line = OrderLine(order_id=OID, sku_id=SkuId("A"), ordered=5, allocated=5, picked=0, shipped=0)
    with pytest.raises(ReturnNotAllowed):
        await create_return(
            _uow(order, [line], FakeReceiptRepo()),
            CreateReturnCommand(OID, ((SkuId("A"), 1),), KEY),
            now=_now,
            new_receipt_id=lambda: RID,
        )


async def test_create_return_over_shipped_qty_rejected() -> None:
    order, lines = _shipped_order(A=5)
    with pytest.raises(ReturnNotAllowed):
        await create_return(
            _uow(order, lines, FakeReceiptRepo()),
            CreateReturnCommand(OID, ((SkuId("A"), 6),), KEY),
            now=_now,
            new_receipt_id=lambda: RID,
        )
