"""Unit tests for the allocate handler's greedy reserve logic (no DB)."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from itertools import count
from uuid import UUID

import pytest

from quartermaster.application.commands import AllocateCommand
from quartermaster.application.errors import OccConflict
from quartermaster.application.handlers.allocate import RESERVATION_TTL, allocate
from quartermaster.application.results import AllocateResult
from quartermaster.domain.errors import IllegalTransition, OrderNotFound
from quartermaster.domain.ids import (
    IdempotencyKey,
    LocationId,
    MovementId,
    OrderId,
    ReservationId,
    SkuId,
)
from quartermaster.domain.movements import MovementType
from quartermaster.domain.orders import Order, OrderLine
from quartermaster.domain.state_machines import OrderState, ReservationState
from tests.unit.fakes import (
    FakeMovementRepo,
    FakeOrderRepo,
    FakeReservationRepo,
    FakeStockRepo,
    FakeUnitOfWork,
)

ORDER = OrderId(UUID("00000000-0000-7000-8000-000000000001"))
KEY = IdempotencyKey("k")


def _fixed_now() -> datetime:
    return datetime(2026, 6, 17, 12, 0, tzinfo=UTC)


def _id_minter(prefix: int) -> Callable[[], UUID]:
    counter = count(1)

    def mint() -> UUID:
        return UUID(int=(prefix << 64) | next(counter))

    return mint


def _res_ids() -> Callable[[], ReservationId]:
    mint = _id_minter(0xAA)
    return lambda: ReservationId(mint())


def _mov_ids() -> Callable[[], MovementId]:
    mint = _id_minter(0xBB)
    return lambda: MovementId(mint())


def _order(state: OrderState, version: int = 1) -> Order:
    return Order(order_id=ORDER, state=state, version=version, created_at=_fixed_now())


def _line(sku: str, ordered: int, allocated: int = 0) -> OrderLine:
    return OrderLine(
        order_id=ORDER, sku_id=SkuId(sku), ordered=ordered, allocated=allocated, picked=0, shipped=0
    )


@dataclass
class _Harness:
    uow: FakeUnitOfWork
    stock: FakeStockRepo
    orders: FakeOrderRepo
    reservations: FakeReservationRepo
    movements: FakeMovementRepo


def _harness(
    *,
    cells: dict[tuple[SkuId, LocationId], int] | None = None,
    order: Order | None = None,
    lines: list[OrderLine] | None = None,
    cas_result: bool = True,
    add_allocated_result: bool = True,
) -> _Harness:
    stock = FakeStockRepo(cells or {})
    orders = FakeOrderRepo(
        order, lines or [], cas_result, add_allocated_result=add_allocated_result
    )
    reservations = FakeReservationRepo()
    movements = FakeMovementRepo()
    uow = FakeUnitOfWork(stock=stock, orders=orders, reservations=reservations, movements=movements)
    return _Harness(uow, stock, orders, reservations, movements)


async def _run(uow: FakeUnitOfWork) -> AllocateResult:
    return await allocate(
        uow,
        AllocateCommand(ORDER, KEY),
        now=_fixed_now,
        new_reservation_id=_res_ids(),
        new_movement_id=_mov_ids(),
    )


async def test_full_allocation_transitions_to_allocated() -> None:
    h = _harness(
        cells={(SkuId("S"), LocationId("L1")): 5},
        order=_order(OrderState.CREATED),
        lines=[_line("S", 5)],
    )
    result = await _run(h.uow)

    assert result.state is OrderState.ALLOCATED
    assert h.orders.allocated == [(ORDER, SkuId("S"), 5)]
    assert len(h.reservations.added) == 1
    res = h.reservations.added[0]
    assert res.qty == 5 and res.state is ReservationState.HELD
    assert res.expires_at == _fixed_now() + RESERVATION_TTL
    assert h.movements.appended[0].type is MovementType.RESERVE
    assert h.movements.appended[0].to_location == LocationId("L1")
    ((_id, expected, _v, target),) = h.orders.cas_calls
    assert expected is OrderState.CREATED and target is OrderState.ALLOCATED


async def test_shortfall_transitions_to_backordered() -> None:
    h = _harness(
        cells={(SkuId("S"), LocationId("L1")): 2},
        order=_order(OrderState.CREATED),
        lines=[_line("S", 5)],
    )
    result = await _run(h.uow)

    assert result.state is OrderState.BACKORDERED
    assert h.orders.allocated == [(ORDER, SkuId("S"), 2)]


async def test_zero_available_backorders_with_no_reservation() -> None:
    h = _harness(cells={}, order=_order(OrderState.CREATED), lines=[_line("S", 5)])
    result = await _run(h.uow)

    assert result.state is OrderState.BACKORDERED
    assert h.reservations.added == []
    assert h.orders.allocated == []
    # A first-time CREATED -> BACKORDERED is a real transition that must persist,
    # so the header CAS still runs even though no line gained allocation.
    ((_id, expected, _v, target),) = h.orders.cas_calls
    assert expected is OrderState.CREATED and target is OrderState.BACKORDERED


async def test_greedy_spans_locations_in_location_order() -> None:
    h = _harness(
        cells={(SkuId("S"), LocationId("L1")): 2, (SkuId("S"), LocationId("L2")): 4},
        order=_order(OrderState.CREATED),
        lines=[_line("S", 5)],
    )
    result = await _run(h.uow)

    assert result.state is OrderState.ALLOCATED
    qtys = [(r.location_id, r.qty) for r in h.reservations.added]
    assert qtys == [
        (LocationId("L1"), 2),
        (LocationId("L2"), 3),
    ]  # L1 drained first, then 3 from L2


async def test_backordered_reallocation_with_nothing_new_skips_the_cas() -> None:
    # A re-swept backordered order that gains no allocation and stays backordered
    # must not be re-CASed: no header write, no dead tuple, no version bump every
    # tick (issue #67). The result still reports the unchanged state.
    h = _harness(cells={}, order=_order(OrderState.BACKORDERED), lines=[_line("S", 5, allocated=0)])
    result = await _run(h.uow)

    assert result.state is OrderState.BACKORDERED
    assert h.orders.cas_calls == []
    assert h.orders.allocated == []


async def test_backordered_partial_progress_still_cases() -> None:
    # When a backordered re-allocation gains *some* allocation but stays short,
    # the header CAS still runs: the version bump serializes concurrent
    # allocate/cancel on an order whose lines just changed.
    h = _harness(
        cells={(SkuId("S"), LocationId("L1")): 2},
        order=_order(OrderState.BACKORDERED),
        lines=[_line("S", 5, allocated=0)],
    )
    result = await _run(h.uow)

    assert result.state is OrderState.BACKORDERED
    assert h.orders.allocated == [(ORDER, SkuId("S"), 2)]
    ((_id, expected, _v, target),) = h.orders.cas_calls
    assert expected is OrderState.BACKORDERED and target is OrderState.BACKORDERED


async def test_missing_order_raises_order_not_found() -> None:
    h = _harness(order=None)
    with pytest.raises(OrderNotFound):
        await _run(h.uow)


async def test_illegal_source_state_raises_illegal_transition() -> None:
    h = _harness(order=_order(OrderState.SHIPPED), lines=[_line("S", 1)])
    with pytest.raises(IllegalTransition):
        await _run(h.uow)


async def test_cas_conflict_raises_occ_conflict() -> None:
    h = _harness(
        cells={(SkuId("S"), LocationId("L1")): 5},
        order=_order(OrderState.CREATED),
        lines=[_line("S", 5)],
        cas_result=False,
    )
    with pytest.raises(OccConflict):
        await _run(h.uow)


async def test_add_allocated_guard_rejection_raises_occ_conflict() -> None:
    """A non-applying add_allocated (guard rejected) must propagate as OccConflict.

    This models the concurrent same-order race where the losing transaction's
    add_allocated guard fires because allocated_qty + take would exceed ordered_qty.
    The envelope retries, re-reads the order as ALLOCATED, and the retry raises
    IllegalTransition — the designed outcome.
    """
    h = _harness(
        cells={(SkuId("S"), LocationId("L1")): 5},
        order=_order(OrderState.CREATED),
        lines=[_line("S", 5)],
        add_allocated_result=False,
    )
    with pytest.raises(OccConflict):
        await _run(h.uow)
