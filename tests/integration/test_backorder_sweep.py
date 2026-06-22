"""Backorder fulfilment sweep on real Postgres (design §5.5, §7)."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncEngine

from quartermaster.adapters.postgres.identifiers import new_movement_id, new_reservation_id
from quartermaster.adapters.postgres.tables import orders, reservation, stock
from quartermaster.adapters.postgres.unit_of_work import postgres_uow_factory
from quartermaster.application.clock import system_clock
from quartermaster.application.handlers.allocate import run_allocate
from quartermaster.domain.ids import IdempotencyKey, OrderId
from quartermaster.domain.state_machines import OrderState
from quartermaster.workers.backorder_sweep import sweep_backorders
from tests.integration.seed import assert_invariants, seed_order, seed_sku_locations_stock


async def _sweep(engine: AsyncEngine) -> object:
    return await sweep_backorders(
        postgres_uow_factory(engine),
        now=system_clock,
        new_reservation_id=new_reservation_id,
        new_movement_id=new_movement_id,
        batch_size=100,
    )


async def _order_state(engine: AsyncEngine, order_id: OrderId) -> str:
    async with engine.connect() as conn:
        return str(
            (
                await conn.execute(select(orders.c.state).where(orders.c.order_id == order_id))
            ).scalar_one()
        )


async def _order_version(engine: AsyncEngine, order_id: OrderId) -> int:
    async with engine.connect() as conn:
        return int(
            (
                await conn.execute(select(orders.c.version).where(orders.c.order_id == order_id))
            ).scalar_one()
        )


async def _reserved(engine: AsyncEngine, sku: str) -> int:
    async with engine.connect() as conn:
        return int(
            (
                await conn.execute(
                    select(func.coalesce(func.sum(stock.c.qty_reserved), 0)).where(
                        stock.c.sku_id == sku
                    )
                )
            ).scalar_one()
        )


async def test_backordered_order_allocated_after_stock(committed_db: AsyncEngine) -> None:
    sku = await seed_sku_locations_stock(committed_db, "S", {"L1": 5})
    order_id = await seed_order(committed_db, state=OrderState.BACKORDERED, lines={"S": 5})

    run = await _sweep(committed_db)

    assert run.scanned == 1 and run.allocated == 1 and run.still_backordered == 0  # type: ignore[attr-defined]
    assert await _order_state(committed_db, order_id) == "allocated"
    assert await _reserved(committed_db, "S") == 5
    await assert_invariants(committed_db, sku)


async def test_short_stock_stays_backordered(committed_db: AsyncEngine) -> None:
    sku = await seed_sku_locations_stock(committed_db, "S", {"L1": 2})
    order_id = await seed_order(committed_db, state=OrderState.BACKORDERED, lines={"S": 5})

    run = await _sweep(committed_db)

    assert run.scanned == 1 and run.allocated == 0 and run.still_backordered == 1  # type: ignore[attr-defined]
    assert await _order_state(committed_db, order_id) == "backordered"
    assert await _reserved(committed_db, "S") == 2  # topped up to what was available
    await assert_invariants(committed_db, sku)


async def test_fifo_older_order_served_first(committed_db: AsyncEngine) -> None:
    sku = await seed_sku_locations_stock(committed_db, "S", {"L1": 5})
    older = await seed_order(
        committed_db,
        state=OrderState.BACKORDERED,
        lines={"S": 5},
        created_at=datetime(2026, 6, 20, 10, 0, tzinfo=UTC),
    )
    younger = await seed_order(
        committed_db,
        state=OrderState.BACKORDERED,
        lines={"S": 5},
        created_at=datetime(2026, 6, 20, 11, 0, tzinfo=UTC),
    )

    run = await _sweep(committed_db)

    assert run.scanned == 2 and run.allocated == 1 and run.still_backordered == 1  # type: ignore[attr-defined]
    assert await _order_state(committed_db, older) == "allocated"
    assert await _order_state(committed_db, younger) == "backordered"
    await assert_invariants(committed_db, sku)


async def test_sweep_vs_live_allocate_no_oversell(committed_db: AsyncEngine) -> None:
    sku = await seed_sku_locations_stock(committed_db, "S", {"L1": 4})
    await seed_order(committed_db, state=OrderState.BACKORDERED, lines={"S": 5})
    live = await seed_order(committed_db, state=OrderState.CREATED, lines={"S": 5})
    factory = postgres_uow_factory(committed_db)

    async def sweep() -> None:
        await sweep_backorders(
            factory,
            now=system_clock,
            new_reservation_id=new_reservation_id,
            new_movement_id=new_movement_id,
            batch_size=100,
        )

    async def live_allocate() -> None:
        await run_allocate(
            factory,
            live,
            IdempotencyKey("live-key"),
            now=system_clock,
            new_reservation_id=new_reservation_id,
            new_movement_id=new_movement_id,
        )

    await asyncio.gather(sweep(), live_allocate(), return_exceptions=True)

    assert await _reserved(committed_db, "S") == 4  # all 4 units reserved, never more (no oversell)
    await assert_invariants(committed_db, sku)


async def test_zero_stock_sweep_does_not_rewrite_the_order(committed_db: AsyncEngine) -> None:
    # A backordered order on a SKU with no stock gains nothing each tick. The
    # sweep must leave the header untouched -- no version bump, no dead tuple --
    # rather than re-CAS it forever (issue #67).
    sku = await seed_sku_locations_stock(committed_db, "S", {})
    order_id = await seed_order(committed_db, state=OrderState.BACKORDERED, lines={"S": 5})
    before = await _order_version(committed_db, order_id)

    for _ in range(3):
        run = await _sweep(committed_db)
        assert run.scanned == 1 and run.allocated == 0 and run.still_backordered == 1  # type: ignore[attr-defined]

    assert await _order_version(committed_db, order_id) == before  # no write across 3 ticks
    assert await _order_state(committed_db, order_id) == "backordered"
    await assert_invariants(committed_db, sku)


async def test_cancelled_order_is_not_swept(committed_db: AsyncEngine) -> None:
    await seed_sku_locations_stock(committed_db, "S", {"L1": 5})
    order_id = await seed_order(committed_db, state=OrderState.CANCELLED, lines={"S": 5})

    run = await _sweep(committed_db)

    assert run.scanned == 0  # type: ignore[attr-defined]  # backordered_orders filters by state; cancelled is invisible
    async with committed_db.connect() as conn:
        res = (
            await conn.execute(
                select(reservation.c.reservation_id).where(reservation.c.order_id == order_id)
            )
        ).all()
    assert res == []
