"""Unit tests for the reservation-expiry reaper pass (fakes; no DB)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

from quartermaster.domain.ids import LocationId, MovementId, OrderId, ReservationId, SkuId
from quartermaster.domain.movements import MovementType
from quartermaster.domain.reservations import Reservation
from quartermaster.domain.state_machines import ReservationState
from quartermaster.workers.reservation_reaper import reap_reservations
from tests.unit.fakes import (
    FakeReservationRepo,
    FakeStockRepo,
    FakeUnitOfWork,
    fake_factory,
)

_NOW = datetime(2026, 6, 20, 12, 0, tzinfo=UTC)


def _due_reservation() -> Reservation:
    return Reservation(
        reservation_id=ReservationId(uuid4()),
        order_id=OrderId(uuid4()),
        sku_id=SkuId("S"),
        location_id=LocationId("L1"),
        qty=3,
        state=ReservationState.HELD,
        expires_at=_NOW - timedelta(minutes=20),
    )


def _movement_id() -> MovementId:
    return MovementId(uuid4())


async def test_expires_due_reservation() -> None:
    res = _due_reservation()
    uow = FakeUnitOfWork(
        reservations=FakeReservationRepo(due=[res]),
        stock=FakeStockRepo(),
    )
    run = await reap_reservations(
        fake_factory(uow), now=lambda: _NOW, new_movement_id=_movement_id, batch_size=500
    )

    assert run.scanned == 1 and run.acted == 1 and run.errors == 0
    reservations = uow.reservations
    assert isinstance(reservations, FakeReservationRepo)
    assert reservations.transitions == [
        (res.reservation_id, ReservationState.HELD, ReservationState.EXPIRED)
    ]
    stock = uow.stock
    assert isinstance(stock, FakeStockRepo)
    assert stock.release_calls == [(res.sku_id, res.location_id, res.qty)]
    movements = uow.movements
    appended = movements.appended  # type: ignore[attr-defined]
    assert len(appended) == 1
    mv = appended[0]
    assert mv.type is MovementType.EXPIRE
    assert mv.command_id == f"reaper:expire:{res.reservation_id}"
    assert mv.qty == 3 and mv.from_location == res.location_id and mv.ref == res.order_id


async def test_already_finalised_reservation_is_a_noop() -> None:
    res = _due_reservation()
    uow = FakeUnitOfWork(
        reservations=FakeReservationRepo(due=[res], transition_result=False),
        stock=FakeStockRepo(),
    )
    run = await reap_reservations(
        fake_factory(uow), now=lambda: _NOW, new_movement_id=_movement_id, batch_size=500
    )

    assert run.scanned == 1 and run.acted == 0 and run.errors == 0
    stock = uow.stock
    assert isinstance(stock, FakeStockRepo)
    assert stock.release_calls == []  # lost the CAS: no release, no movement
    assert uow.movements.appended == []  # type: ignore[attr-defined]


async def test_held_but_missing_stock_is_counted_as_error() -> None:
    res = _due_reservation()
    uow = FakeUnitOfWork(
        reservations=FakeReservationRepo(due=[res]),
        stock=FakeStockRepo(),
    )
    stock = uow.stock
    assert isinstance(stock, FakeStockRepo)
    stock.release_result = False  # held row but stock cell gone -> InvariantViolation

    run = await reap_reservations(
        fake_factory(uow), now=lambda: _NOW, new_movement_id=_movement_id, batch_size=500
    )

    assert run.scanned == 1 and run.acted == 0 and run.errors == 1  # caught, pass continues
    assert uow.movements.appended == []  # type: ignore[attr-defined]


async def test_no_due_reservations() -> None:
    uow = FakeUnitOfWork(reservations=FakeReservationRepo(due=[]))
    run = await reap_reservations(
        fake_factory(uow), now=lambda: _NOW, new_movement_id=_movement_id, batch_size=500
    )
    assert run == run.__class__(scanned=0, acted=0, errors=0)
