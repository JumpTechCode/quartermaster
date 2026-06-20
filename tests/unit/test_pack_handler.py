"""Unit tests for the pack handler (document transition, no stock)."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

import pytest

from quartermaster.application.commands import PackCommand
from quartermaster.application.errors import OccConflict
from quartermaster.application.handlers.pack import pack
from quartermaster.application.results import PackResult
from quartermaster.domain.errors import IllegalTransition, OrderNotFound
from quartermaster.domain.ids import IdempotencyKey, OrderId
from quartermaster.domain.orders import Order
from quartermaster.domain.state_machines import OrderState
from tests.unit.fakes import FakeOrderRepo, FakeUnitOfWork

ORDER = OrderId(UUID("00000000-0000-7000-8000-000000000001"))
KEY = IdempotencyKey("k")


def _order(state: OrderState, version: int = 1) -> Order:
    return Order(
        order_id=ORDER, state=state, version=version, created_at=datetime(2026, 6, 20, tzinfo=UTC)
    )


def _uow(*, order: Order | None, cas_result: bool = True) -> FakeUnitOfWork:
    return FakeUnitOfWork(orders=FakeOrderRepo(order=order, cas_result=cas_result))


async def _run(uow: FakeUnitOfWork) -> PackResult:
    return await pack(uow, PackCommand(ORDER, KEY))


async def test_pack_advances_to_packed() -> None:
    orders = FakeOrderRepo(order=_order(OrderState.PICKED))
    uow = FakeUnitOfWork(orders=orders)
    result = await _run(uow)
    assert result.state is OrderState.PACKED
    assert [(c[1], c[2], c[3]) for c in orders.cas_calls] == [
        (OrderState.PICKED, 1, OrderState.PACKED)
    ]


async def test_pack_missing_order_raises_order_not_found() -> None:
    with pytest.raises(OrderNotFound):
        await _run(_uow(order=None))


async def test_pack_from_non_picked_raises_illegal_transition() -> None:
    with pytest.raises(IllegalTransition):
        await _run(_uow(order=_order(OrderState.ALLOCATED)))


async def test_pack_cas_conflict_raises_occ_conflict() -> None:
    with pytest.raises(OccConflict):
        await _run(_uow(order=_order(OrderState.PICKED), cas_result=False))
