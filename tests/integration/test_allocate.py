# tests/integration/test_allocate.py
"""Single-threaded behavioral tests of the allocate command on real Postgres."""

from __future__ import annotations

from collections.abc import Callable, Coroutine
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine

from quartermaster.adapters.postgres.identifiers import new_movement_id, new_reservation_id
from quartermaster.adapters.postgres.tables import (
    idempotency_key,
    movement,
    orders,
    reservation,
    stock,
)
from quartermaster.adapters.postgres.unit_of_work import postgres_uow_factory
from quartermaster.application.clock import system_clock
from quartermaster.application.handlers.allocate import run_allocate
from quartermaster.application.results import AllocateResult
from quartermaster.domain.errors import IdempotencyKeyReuse, IllegalTransition
from quartermaster.domain.ids import IdempotencyKey, OrderId
from quartermaster.domain.state_machines import OrderState
from tests.integration.seed import assert_invariants, seed_order, seed_sku_locations_stock


def _runner(
    engine: AsyncEngine,
) -> Callable[[OrderId, str], Coroutine[Any, Any, AllocateResult]]:
    factory = postgres_uow_factory(engine)

    async def run(order_id: OrderId, key: str) -> AllocateResult:
        return await run_allocate(
            factory,
            order_id,
            IdempotencyKey(key),
            now=system_clock,
            new_reservation_id=new_reservation_id,
            new_movement_id=new_movement_id,
        )

    return run


async def test_full_allocation(committed_db: AsyncEngine) -> None:
    sku = await seed_sku_locations_stock(committed_db, "S", {"L1": 5})
    order_id = await seed_order(committed_db, state=OrderState.CREATED, lines={"S": 5})
    result = await _runner(committed_db)(order_id, "k1")

    assert result.state is OrderState.ALLOCATED
    async with committed_db.connect() as conn:
        state = (
            await conn.execute(select(orders.c.state).where(orders.c.order_id == order_id))
        ).scalar_one()
        assert state == "allocated"

        qty_reserved = (
            await conn.execute(
                select(stock.c.qty_reserved).where(
                    (stock.c.sku_id == "S") & (stock.c.location_id == "L1")
                )
            )
        ).scalar_one()
        assert qty_reserved == 5

        movement_rows = (
            await conn.execute(
                select(movement.c.type, movement.c.qty, movement.c.ref).where(
                    movement.c.sku_id == "S"
                )
            )
        ).all()
        assert len(movement_rows) == 1
        assert movement_rows[0].type == "reserve"
        assert movement_rows[0].qty == 5
        assert movement_rows[0].ref == order_id

        key_status = (
            await conn.execute(
                select(idempotency_key.c.status).where(idempotency_key.c.key == "k1")
            )
        ).scalar_one()
        assert key_status == "succeeded"

    await assert_invariants(committed_db, sku)


async def test_partial_allocation_backorders(committed_db: AsyncEngine) -> None:
    sku = await seed_sku_locations_stock(committed_db, "S", {"L1": 2})
    order_id = await seed_order(committed_db, state=OrderState.CREATED, lines={"S": 5})
    result = await _runner(committed_db)(order_id, "k1")

    assert result.state is OrderState.BACKORDERED
    await assert_invariants(committed_db, sku)


async def test_greedy_across_two_locations(committed_db: AsyncEngine) -> None:
    sku = await seed_sku_locations_stock(committed_db, "S", {"L1": 2, "L2": 4})
    order_id = await seed_order(committed_db, state=OrderState.CREATED, lines={"S": 5})
    result = await _runner(committed_db)(order_id, "k1")

    assert result.state is OrderState.ALLOCATED
    async with committed_db.connect() as conn:
        rows = (
            await conn.execute(
                select(reservation.c.location_id, reservation.c.qty)
                .where(reservation.c.order_id == order_id)
                .order_by(reservation.c.location_id)
            )
        ).all()
    assert [(r.location_id, r.qty) for r in rows] == [("L1", 2), ("L2", 3)]
    await assert_invariants(committed_db, sku)


async def test_sequential_replay_is_one_effect(committed_db: AsyncEngine) -> None:
    sku = await seed_sku_locations_stock(committed_db, "S", {"L1": 5})
    order_id = await seed_order(committed_db, state=OrderState.CREATED, lines={"S": 5})
    run = _runner(committed_db)
    first = await run(order_id, "same-key")
    second = await run(order_id, "same-key")  # replays

    assert first == second
    async with committed_db.connect() as conn:
        count = (
            await conn.execute(
                select(reservation.c.reservation_id).where(reservation.c.order_id == order_id)
            )
        ).all()
    assert len(count) == 1  # exactly one reservation, not two
    await assert_invariants(committed_db, sku)


async def test_allocate_from_illegal_state_is_rejected(committed_db: AsyncEngine) -> None:
    await seed_sku_locations_stock(committed_db, "S", {"L1": 5})
    order_id = await seed_order(committed_db, state=OrderState.SHIPPED, lines={"S": 5})
    with pytest.raises(IllegalTransition):
        await _runner(committed_db)(order_id, "k1")
    # the rejection is cached: a replay with the same key returns the same rejection
    with pytest.raises(IllegalTransition):
        await _runner(committed_db)(order_id, "k1")


async def test_key_reuse_with_different_order_is_rejected(committed_db: AsyncEngine) -> None:
    await seed_sku_locations_stock(committed_db, "S", {"L1": 5})
    order_a = await seed_order(committed_db, state=OrderState.CREATED, lines={"S": 1})
    order_b = await seed_order(committed_db, state=OrderState.CREATED, lines={"S": 1})
    run = _runner(committed_db)
    await run(order_a, "shared")
    with pytest.raises(IdempotencyKeyReuse):
        await run(order_b, "shared")  # same key, different fingerprint
