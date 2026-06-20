"""Unit tests for the read-path load_order query over fakes."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

from quartermaster.application.queries import load_order, load_receipt
from quartermaster.domain.ids import OrderId, ReceiptId, SkuId
from quartermaster.domain.orders import Order, OrderLine
from quartermaster.domain.receipts import Receipt, ReceiptKind, ReceiptLine
from quartermaster.domain.state_machines import OrderState, ReceiptState
from tests.unit.fakes import FakeOrderRepo, FakeReceiptRepo, FakeUnitOfWork, fake_factory

_OID = OrderId(UUID("00000000-0000-7000-8000-000000000001"))
_FIXED = datetime(2026, 6, 18, tzinfo=UTC)


async def test_load_order_returns_view() -> None:
    order = Order(order_id=_OID, state=OrderState.BACKORDERED, version=2, created_at=_FIXED)
    line = OrderLine(order_id=_OID, sku_id=SkuId("A"), ordered=5, allocated=3, picked=0, shipped=0)
    uow = FakeUnitOfWork(orders=FakeOrderRepo(order=order, lines=[line]))

    view = await load_order(fake_factory(uow), _OID)

    assert uow.commits == 0
    assert view is not None
    assert view.state is OrderState.BACKORDERED and view.version == 2
    assert view.lines == (line,)


async def test_load_order_missing_returns_none() -> None:
    uow = FakeUnitOfWork(orders=FakeOrderRepo(order=None))
    assert await load_order(fake_factory(uow), _OID) is None


_RID = ReceiptId(UUID("00000000-0000-7000-8000-000000000005"))


async def test_load_receipt_returns_view() -> None:
    receipt = Receipt(
        _RID,
        ReceiptKind.SUPPLIER_RECEIPT,
        ReceiptState.EXPECTED,
        1,
        datetime(2026, 6, 20, tzinfo=UTC),
        None,
    )
    line = ReceiptLine(_RID, SkuId("A"), 5, 0)
    uow = FakeUnitOfWork(receipts=FakeReceiptRepo(receipt=receipt, lines=[line]))
    view = await load_receipt(fake_factory(uow), _RID)
    assert view is not None
    assert view.kind is ReceiptKind.SUPPLIER_RECEIPT
    assert view.state is ReceiptState.EXPECTED
    assert view.lines == (line,)


async def test_load_receipt_missing_returns_none() -> None:
    uow = FakeUnitOfWork(receipts=FakeReceiptRepo(receipt=None))
    assert await load_receipt(fake_factory(uow), _RID) is None
