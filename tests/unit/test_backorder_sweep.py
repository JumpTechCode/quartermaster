"""Unit tests for the backorder-sweep pass (fakes; no DB)."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

from quartermaster.domain.ids import LocationId, MovementId, OrderId, ReservationId, SkuId
from quartermaster.domain.orders import Order, OrderLine
from quartermaster.domain.state_machines import OrderState
from quartermaster.workers.backorder_sweep import sweep_backorders
from tests.unit.fakes import FakeOrderRepo, FakeStockRepo, FakeUnitOfWork, fake_factory

_NOW = datetime(2026, 6, 20, 12, 0, tzinfo=UTC)


def _ids() -> tuple[OrderId, ReservationId, MovementId]:
    return OrderId(uuid4()), ReservationId(uuid4()), MovementId(uuid4())


def _backordered_order(order_id: OrderId) -> Order:
    return Order(order_id=order_id, state=OrderState.BACKORDERED, version=1, created_at=_NOW)


def _line(order_id: OrderId, ordered: int) -> OrderLine:
    return OrderLine(
        order_id=order_id, sku_id=SkuId("S"), ordered=ordered, allocated=0, picked=0, shipped=0
    )


async def test_satisfiable_order_is_reallocated() -> None:
    order_id, _, _ = _ids()
    uow = FakeUnitOfWork(
        orders=FakeOrderRepo(
            order=_backordered_order(order_id), lines=[_line(order_id, 5)], backordered=[order_id]
        ),
        stock=FakeStockRepo(cells={(SkuId("S"), LocationId("L1")): 5}),
    )
    run = await sweep_backorders(
        fake_factory(uow),
        now=lambda: _NOW,
        new_reservation_id=lambda: ReservationId(uuid4()),
        new_movement_id=lambda: MovementId(uuid4()),
        batch_size=100,
    )

    assert run.scanned == 1 and run.allocated == 1
    assert run.still_backordered == 0 and run.errors == 0


async def test_short_stock_stays_backordered() -> None:
    order_id, _, _ = _ids()
    uow = FakeUnitOfWork(
        orders=FakeOrderRepo(
            order=_backordered_order(order_id), lines=[_line(order_id, 5)], backordered=[order_id]
        ),
        stock=FakeStockRepo(cells={(SkuId("S"), LocationId("L1")): 2}),
    )
    run = await sweep_backorders(
        fake_factory(uow),
        now=lambda: _NOW,
        new_reservation_id=lambda: ReservationId(uuid4()),
        new_movement_id=lambda: MovementId(uuid4()),
        batch_size=100,
    )

    assert run.scanned == 1 and run.allocated == 0
    assert run.still_backordered == 1 and run.errors == 0


async def test_order_changed_under_sweep_is_counted_as_error() -> None:
    order_id, _, _ = _ids()
    changed = Order(order_id=order_id, state=OrderState.ALLOCATED, version=1, created_at=_NOW)
    uow = FakeUnitOfWork(
        orders=FakeOrderRepo(order=changed, lines=[_line(order_id, 5)], backordered=[order_id]),
        stock=FakeStockRepo(cells={(SkuId("S"), LocationId("L1")): 5}),
    )
    run = await sweep_backorders(
        fake_factory(uow),
        now=lambda: _NOW,
        new_reservation_id=lambda: ReservationId(uuid4()),
        new_movement_id=lambda: MovementId(uuid4()),
        batch_size=100,
    )

    # allocate raised IllegalTransition
    assert run.scanned == 1 and run.allocated == 0 and run.errors == 1
    reservations = uow.reservations
    assert reservations.added == []  # type: ignore[attr-defined]


async def test_no_backordered_orders() -> None:
    uow = FakeUnitOfWork(orders=FakeOrderRepo(backordered=[]))
    run = await sweep_backorders(
        fake_factory(uow),
        now=lambda: _NOW,
        new_reservation_id=lambda: ReservationId(uuid4()),
        new_movement_id=lambda: MovementId(uuid4()),
        batch_size=100,
    )
    assert run == run.__class__(scanned=0, allocated=0, still_backordered=0, errors=0)
