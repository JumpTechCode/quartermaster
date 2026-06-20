"""Unit tests for the read-path load_order query over fakes."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

from quartermaster.application.queries import load_order
from quartermaster.domain.ids import OrderId, SkuId
from quartermaster.domain.orders import Order, OrderLine
from quartermaster.domain.state_machines import OrderState
from tests.unit.fakes import FakeOrderRepo, FakeUnitOfWork, fake_factory

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
