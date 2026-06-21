"""Reaper order de-allocation on real Postgres (slice 2/2-B; design §4, §5.4, §7)."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncEngine

from quartermaster.adapters.postgres.identifiers import new_movement_id, new_reservation_id
from quartermaster.adapters.postgres.tables import order_line, orders, reservation, stock
from quartermaster.adapters.postgres.unit_of_work import postgres_uow_factory
from quartermaster.application.clock import system_clock
from quartermaster.application.handlers.pick import run_pick
from quartermaster.domain.ids import IdempotencyKey, OrderId, SkuId
from quartermaster.domain.state_machines import OrderState
from quartermaster.workers.backorder_sweep import sweep_backorders
from quartermaster.workers.reservation_reaper import reap_reservations
from tests.integration.seed import (
    assert_invariants,
    seed_held_reservation,
    seed_order,
    seed_sku_locations_stock,
)

_PAST = timedelta(minutes=20)


async def _state(engine: AsyncEngine, order_id: OrderId) -> str:
    async with engine.connect() as conn:
        return str(
            (
                await conn.execute(select(orders.c.state).where(orders.c.order_id == order_id))
            ).scalar_one()
        )


async def _allocated_qty(engine: AsyncEngine, order_id: OrderId) -> int:
    async with engine.connect() as conn:
        return int(
            (
                await conn.execute(
                    select(order_line.c.allocated_qty).where(order_line.c.order_id == order_id)
                )
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


async def _reap(engine: AsyncEngine) -> object:
    return await reap_reservations(
        postgres_uow_factory(engine),
        now=system_clock,
        new_movement_id=new_movement_id,
        batch_size=500,
    )


async def _sweep(engine: AsyncEngine) -> object:
    return await sweep_backorders(
        postgres_uow_factory(engine),
        now=system_clock,
        new_reservation_id=new_reservation_id,
        new_movement_id=new_movement_id,
        batch_size=100,
    )


async def test_reaper_then_sweep_reallocates_the_order(committed_db: AsyncEngine) -> None:
    """The gap, closed: a reaped allocated order is re-opened and re-allocated."""
    sku = await seed_sku_locations_stock(committed_db, "S", {"L1": 3})
    order_id = await seed_order(
        committed_db, state=OrderState.ALLOCATED, lines={"S": 3}, allocated={"S": 3}
    )
    await seed_held_reservation(
        committed_db,
        sku=sku,
        location="L1",
        order_id=order_id,
        qty=3,
        expires_at=datetime.now(UTC) - _PAST,
    )

    reap = await _reap(committed_db)
    assert reap.acted == 1 and reap.reopened == 1  # type: ignore[attr-defined]
    assert await _state(committed_db, order_id) == "backordered"
    assert await _allocated_qty(committed_db, order_id) == 0
    assert await _reserved(committed_db, "S") == 0  # stock freed back to available

    sweep = await _sweep(committed_db)
    assert sweep.allocated == 1  # type: ignore[attr-defined]
    assert await _state(committed_db, order_id) == "allocated"
    assert await _allocated_qty(committed_db, order_id) == 3
    assert await _reserved(committed_db, "S") == 3  # re-reserved from the freed stock

    async with committed_db.connect() as conn:
        held = (
            await conn.execute(
                select(func.count())
                .select_from(reservation)
                .where(
                    reservation.c.order_id == order_id,
                    reservation.c.state == "held",
                )
            )
        ).scalar_one()
    assert held == 1  # exactly one fresh held reservation
    await assert_invariants(committed_db, sku)


async def test_reaper_versus_pick_one_effect(committed_db: AsyncEngine) -> None:
    """Reaper and pick race the same allocated order: exactly one effect, no oversell."""
    sku = await seed_sku_locations_stock(committed_db, "S", {"L1": 3})
    order_id = await seed_order(
        committed_db, state=OrderState.ALLOCATED, lines={"S": 3}, allocated={"S": 3}
    )
    res_id = await seed_held_reservation(
        committed_db,
        sku=sku,
        location="L1",
        order_id=order_id,
        qty=3,
        expires_at=datetime.now(UTC) - _PAST,
    )
    factory = postgres_uow_factory(committed_db)

    async def reap() -> None:
        await reap_reservations(
            factory, now=system_clock, new_movement_id=new_movement_id, batch_size=500
        )

    async def pick() -> None:
        await run_pick(
            factory,
            order_id,
            IdempotencyKey("pick-key"),
            now=system_clock,
            new_movement_id=new_movement_id,
        )

    # The reservation-state CAS is the single arbiter; whichever interleaving wins,
    # the reservation is finalised exactly once and the stock is freed-or-consumed
    # exactly once. The losing command raises (caught here).
    await asyncio.gather(reap(), pick(), return_exceptions=True)

    async with committed_db.connect() as conn:
        res_state = str(
            (
                await conn.execute(
                    select(reservation.c.state).where(reservation.c.reservation_id == res_id)
                )
            ).scalar_one()
        )
    assert res_state in {"expired", "consumed"}
    assert await _reserved(committed_db, "S") == 0  # released or consumed once, never negative
    assert await _state(committed_db, order_id) in {"backordered", "picked"}
    await assert_invariants(committed_db, sku)


async def test_remove_allocated_guard_rejects_below_picked(committed_db: AsyncEngine) -> None:
    await seed_sku_locations_stock(committed_db, "S", {"L1": 5})
    order_id = await seed_order(
        committed_db,
        state=OrderState.ALLOCATED,
        lines={"S": 5},
        allocated={"S": 3},
        picked={"S": 2},
    )
    factory = postgres_uow_factory(committed_db)

    async with factory() as uow:
        rejected = await uow.orders.remove_allocated(order_id, SkuId("S"), 2)  # 3 - 2 < 2
        await uow.commit()
    assert rejected is False
    assert await _allocated_qty(committed_db, order_id) == 3  # unchanged

    async with factory() as uow:
        ok = await uow.orders.remove_allocated(order_id, SkuId("S"), 1)  # 3 - 1 >= 2
        await uow.commit()
    assert ok is True
    assert await _allocated_qty(committed_db, order_id) == 2


async def test_mark_backordered_only_flips_allocated(committed_db: AsyncEngine) -> None:
    await seed_sku_locations_stock(committed_db, "S", {"L1": 5})
    allocated_order = await seed_order(
        committed_db, state=OrderState.ALLOCATED, lines={"S": 1}, allocated={"S": 1}
    )
    picking_order = await seed_order(
        committed_db, state=OrderState.PICKING, lines={"S": 1}, allocated={"S": 1}
    )
    factory = postgres_uow_factory(committed_db)

    async with factory() as uow:
        flipped = await uow.orders.mark_backordered(allocated_order)
        not_flipped = await uow.orders.mark_backordered(picking_order)
        await uow.commit()

    assert flipped is True
    assert not_flipped is False
    assert await _state(committed_db, allocated_order) == "backordered"
    assert await _state(committed_db, picking_order) == "picking"
