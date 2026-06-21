"""Unit tests for the ship handler (document CAS + shipped_qty finalize)."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

import pytest

from quartermaster.application.commands import ShipCommand
from quartermaster.application.errors import OccConflict
from quartermaster.application.handlers.ship import ship
from quartermaster.application.results import ShipResult
from quartermaster.domain.errors import IllegalTransition, OrderNotFound
from quartermaster.domain.ids import IdempotencyKey, OrderId, SkuId
from quartermaster.domain.orders import Order, OrderLine
from quartermaster.domain.state_machines import OrderState
from tests.unit.fakes import FakeOrderRepo, FakeUnitOfWork

ORDER = OrderId(UUID("00000000-0000-7000-8000-000000000001"))
KEY = IdempotencyKey("k")


def _order(state: OrderState, version: int = 1) -> Order:
    return Order(
        order_id=ORDER, state=state, version=version, created_at=datetime(2026, 6, 20, tzinfo=UTC)
    )


def _line(sku: str, picked: int, shipped: int = 0) -> OrderLine:
    return OrderLine(
        order_id=ORDER,
        sku_id=SkuId(sku),
        ordered=picked,
        allocated=picked,
        picked=picked,
        shipped=shipped,
    )


async def _run(uow: FakeUnitOfWork) -> ShipResult:
    return await ship(uow, ShipCommand(ORDER, KEY))


async def test_ship_finalizes_shipped_per_line() -> None:
    orders = FakeOrderRepo(order=_order(OrderState.PACKED), lines=[_line("A", 5), _line("B", 2)])
    uow = FakeUnitOfWork(orders=orders)
    result = await _run(uow)
    assert result.state is OrderState.SHIPPED
    assert [(line.sku_id, line.shipped) for line in result.lines] == [("A", 5), ("B", 2)]
    assert orders.shipped == [(ORDER, SkuId("A"), 5), (ORDER, SkuId("B"), 2)]
    assert [(c[1], c[3]) for c in orders.cas_calls] == [(OrderState.PACKED, OrderState.SHIPPED)]


async def test_ship_reports_quantity_shipped_not_cumulative_picked() -> None:
    # A line already part-shipped (picked=5, shipped=2) ships its outstanding 3.
    # The result must report 3 (what this command shipped), not the cumulative 5.
    orders = FakeOrderRepo(order=_order(OrderState.PACKED), lines=[_line("A", 5, shipped=2)])
    uow = FakeUnitOfWork(orders=orders)
    result = await _run(uow)
    assert [(line.sku_id, line.shipped) for line in result.lines] == [("A", 3)]
    assert orders.shipped == [(ORDER, SkuId("A"), 3)]


async def test_ship_omits_fully_backordered_zero_picked_lines() -> None:
    # A multi-line order where one line was fully backordered (picked=0) ships
    # nothing for it: the result reports only the lines this command shipped,
    # and no add_shipped is issued for the zero-quantity line.
    orders = FakeOrderRepo(order=_order(OrderState.PACKED), lines=[_line("A", 5), _line("B", 0)])
    uow = FakeUnitOfWork(orders=orders)
    result = await _run(uow)
    assert [(line.sku_id, line.shipped) for line in result.lines] == [("A", 5)]
    assert orders.shipped == [(ORDER, SkuId("A"), 5)]


async def test_ship_missing_order_raises_order_not_found() -> None:
    with pytest.raises(OrderNotFound):
        await _run(FakeUnitOfWork(orders=FakeOrderRepo(order=None)))


async def test_ship_from_non_packed_raises_illegal_transition() -> None:
    with pytest.raises(IllegalTransition):
        await _run(
            FakeUnitOfWork(
                orders=FakeOrderRepo(order=_order(OrderState.PICKED), lines=[_line("A", 5)])
            )
        )


async def test_ship_cas_conflict_raises_occ_conflict() -> None:
    with pytest.raises(OccConflict):
        await _run(
            FakeUnitOfWork(
                orders=FakeOrderRepo(
                    order=_order(OrderState.PACKED), lines=[_line("A", 5)], cas_result=False
                )
            )
        )


async def test_ship_add_shipped_guard_rejection_raises_occ_conflict() -> None:
    # cas_state passes (default True) so the per-line loop runs; add_shipped then rejects.
    orders = FakeOrderRepo(
        order=_order(OrderState.PACKED), lines=[_line("A", 5)], add_shipped_result=False
    )
    with pytest.raises(OccConflict):
        await _run(FakeUnitOfWork(orders=orders))
