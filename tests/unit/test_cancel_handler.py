"""Unit tests for the cancel handler's reservation-CAS-gated release (no DB)."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from itertools import count
from uuid import UUID

import pytest

from quartermaster.application.commands import CancelCommand
from quartermaster.application.errors import OccConflict
from quartermaster.application.handlers.cancel import cancel
from quartermaster.application.results import CancelResult
from quartermaster.domain.errors import IllegalTransition, InvariantViolation, OrderNotFound
from quartermaster.domain.ids import (
    IdempotencyKey,
    LocationId,
    MovementId,
    OrderId,
    ReservationId,
    SkuId,
)
from quartermaster.domain.movements import MovementType
from quartermaster.domain.orders import Order
from quartermaster.domain.reservations import Reservation
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


def _now() -> datetime:
    return datetime(2026, 6, 20, 12, 0, tzinfo=UTC)


def _mov_ids() -> Callable[[], MovementId]:
    counter = count(1)
    return lambda: MovementId(UUID(int=(0xBB << 64) | next(counter)))


def _order(state: OrderState, version: int = 1) -> Order:
    return Order(order_id=ORDER, state=state, version=version, created_at=_now())


def _res(sku: str, loc: str, qty: int, rid: int) -> Reservation:
    return Reservation(
        reservation_id=ReservationId(UUID(int=(0xAA << 64) | rid)),
        order_id=ORDER,
        sku_id=SkuId(sku),
        location_id=LocationId(loc),
        qty=qty,
        state=ReservationState.HELD,
        expires_at=_now(),
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
    order: Order | None,
    held: list[Reservation] | None = None,
    cas_result: bool = True,
    transition_result: bool = True,
    release_result: bool = True,
) -> _Harness:
    stock = FakeStockRepo()
    stock.release_result = release_result
    orders = FakeOrderRepo(order=order, cas_result=cas_result)
    reservations = FakeReservationRepo(held or [], transition_result=transition_result)
    movements = FakeMovementRepo()
    uow = FakeUnitOfWork(stock=stock, orders=orders, reservations=reservations, movements=movements)
    return _Harness(uow, stock, orders, reservations, movements)


async def _run(uow: FakeUnitOfWork) -> CancelResult:
    return await cancel(uow, CancelCommand(ORDER, KEY), now=_now, new_movement_id=_mov_ids())


async def test_cancel_from_allocated_releases_reservations() -> None:
    h = _harness(order=_order(OrderState.ALLOCATED), held=[_res("A", "L1", 5, 1)])
    result = await _run(h.uow)
    assert result.state is OrderState.CANCELLED
    assert result.released_reservation_ids == (ReservationId(UUID(int=(0xAA << 64) | 1)),)
    assert h.reservations.transitions == [
        (
            ReservationId(UUID(int=(0xAA << 64) | 1)),
            ReservationState.HELD,
            ReservationState.RELEASED,
        )
    ]
    assert h.stock.release_calls == [(SkuId("A"), LocationId("L1"), 5)]
    mv = h.movements.appended[0]
    assert (
        mv.type is MovementType.RELEASE
        and mv.from_location == LocationId("L1")
        and mv.to_location is None
    )
    assert [(c[1], c[3]) for c in h.orders.cas_calls] == [
        (OrderState.ALLOCATED, OrderState.CANCELLED)
    ]


async def test_cancel_from_created_has_no_reservations() -> None:
    h = _harness(order=_order(OrderState.CREATED), held=[])
    result = await _run(h.uow)
    assert result.state is OrderState.CANCELLED
    assert result.released_reservation_ids == ()
    assert h.stock.release_calls == []
    assert h.movements.appended == []


async def test_cancel_lost_reservation_cas_is_a_noop() -> None:
    h = _harness(
        order=_order(OrderState.ALLOCATED), held=[_res("A", "L1", 5, 1)], transition_result=False
    )
    result = await _run(h.uow)
    assert result.released_reservation_ids == ()
    assert h.stock.release_calls == []
    assert h.movements.appended == []


async def test_cancel_release_miss_after_winning_cas_raises_invariant_violation() -> None:
    h = _harness(
        order=_order(OrderState.ALLOCATED), held=[_res("A", "L1", 5, 1)], release_result=False
    )
    with pytest.raises(InvariantViolation):
        await _run(h.uow)


async def test_cancel_missing_order_raises_order_not_found() -> None:
    with pytest.raises(OrderNotFound):
        await _run(_harness(order=None).uow)


async def test_cancel_from_picking_raises_illegal_transition() -> None:
    with pytest.raises(IllegalTransition):
        await _run(_harness(order=_order(OrderState.PICKING), held=[_res("A", "L1", 5, 1)]).uow)


async def test_cancel_cas_conflict_raises_occ_conflict() -> None:
    with pytest.raises(OccConflict):
        await _run(
            _harness(
                order=_order(OrderState.ALLOCATED), held=[_res("A", "L1", 5, 1)], cas_result=False
            ).uow
        )
