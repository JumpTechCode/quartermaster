"""Behavioural + concurrency tests of the pick command on real Postgres (design §7)."""

from __future__ import annotations

import asyncio

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncEngine

from quartermaster.adapters.postgres.identifiers import (
    new_movement_id,
    new_reservation_id,
)
from quartermaster.adapters.postgres.tables import movement, order_line, orders, stock
from quartermaster.adapters.postgres.unit_of_work import postgres_uow_factory
from quartermaster.application.clock import system_clock
from quartermaster.application.handlers.allocate import run_allocate
from quartermaster.application.handlers.pick import run_pick
from quartermaster.application.results import PickResult
from quartermaster.domain.ids import IdempotencyKey, OrderId
from quartermaster.domain.movements import MovementType
from quartermaster.domain.state_machines import OrderState
from tests.integration.seed import assert_invariants, seed_order, seed_sku_locations_stock


async def _allocate(engine: AsyncEngine, order_id: OrderId, key: str) -> None:
    await run_allocate(
        postgres_uow_factory(engine),
        order_id,
        IdempotencyKey(key),
        now=system_clock,
        new_reservation_id=new_reservation_id,
        new_movement_id=new_movement_id,
    )


async def _pick(engine: AsyncEngine, order_id: OrderId, key: str) -> PickResult:
    return await run_pick(
        postgres_uow_factory(engine),
        order_id,
        IdempotencyKey(key),
        now=system_clock,
        new_movement_id=new_movement_id,
    )


async def test_pick_full_lifecycle_consumes_and_records(committed_db: AsyncEngine) -> None:
    sku = await seed_sku_locations_stock(committed_db, "S", {"L1": 5})
    order_id = await seed_order(committed_db, state=OrderState.CREATED, lines={"S": 5})
    await _allocate(committed_db, order_id, "alloc")

    result = await _pick(committed_db, order_id, "pick")

    assert result.state is OrderState.PICKED
    assert [(line.sku_id, line.picked) for line in result.lines] == [("S", 5)]
    async with committed_db.connect() as conn:
        header = (
            await conn.execute(select(orders.c.state).where(orders.c.order_id == order_id))
        ).scalar_one()
        line = (
            await conn.execute(
                select(order_line.c.picked_qty, order_line.c.allocated_qty).where(
                    order_line.c.order_id == order_id
                )
            )
        ).one()
        cell = (
            await conn.execute(
                select(stock.c.qty_on_hand, stock.c.qty_reserved).where(stock.c.sku_id == sku)
            )
        ).one()
        picks = (
            await conn.execute(
                select(func.count())
                .select_from(movement)
                .where(movement.c.type == MovementType.PICK.value, movement.c.ref == order_id)
            )
        ).scalar_one()
    assert header == "picked"
    assert (line.picked_qty, line.allocated_qty) == (5, 5)
    assert (cell.qty_on_hand, cell.qty_reserved) == (0, 0)  # consumed from the shelf
    assert picks == 1
    await assert_invariants(committed_db, sku)


async def test_n_concurrent_picks_on_hot_sku_never_negative(committed_db: AsyncEngine) -> None:
    # 6 on hand at one cell; 6 orders each reserve 1, then all pick concurrently.
    n = 6
    sku = await seed_sku_locations_stock(committed_db, "S", {"L1": n})
    order_ids = [
        await seed_order(committed_db, state=OrderState.CREATED, lines={"S": 1}) for _ in range(n)
    ]
    for i, oid in enumerate(order_ids):
        await _allocate(committed_db, oid, f"alloc-{i}")

    results = await asyncio.gather(
        *(_pick(committed_db, oid, f"pick-{i}") for i, oid in enumerate(order_ids))
    )

    assert all(r.state is OrderState.PICKED for r in results)
    async with committed_db.connect() as conn:
        cell = (
            await conn.execute(
                select(stock.c.qty_on_hand, stock.c.qty_reserved).where(stock.c.sku_id == sku)
            )
        ).one()
    assert (cell.qty_on_hand, cell.qty_reserved) == (0, 0)  # all 6 picked, never negative
    await assert_invariants(committed_db, sku)


async def test_same_key_pick_fired_concurrently_is_one_consume(committed_db: AsyncEngine) -> None:
    sku = await seed_sku_locations_stock(committed_db, "S", {"L1": 5})
    order_id = await seed_order(committed_db, state=OrderState.CREATED, lines={"S": 5})
    await _allocate(committed_db, order_id, "alloc")

    results = await asyncio.gather(*(_pick(committed_db, order_id, "same-key") for _ in range(8)))

    assert all(r == results[0] for r in results)  # one effect, replayed
    async with committed_db.connect() as conn:
        picks = (
            await conn.execute(
                select(func.count())
                .select_from(movement)
                .where(movement.c.type == MovementType.PICK.value, movement.c.ref == order_id)
            )
        ).scalar_one()
        cell = (
            await conn.execute(select(stock.c.qty_on_hand).where(stock.c.sku_id == sku))
        ).scalar_one()
    assert picks == 1  # exactly one consume despite 8 concurrent calls
    assert cell == 0
    await assert_invariants(committed_db, sku)
