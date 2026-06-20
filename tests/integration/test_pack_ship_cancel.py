"""Behavioural + race tests for pack/ship/cancel on real Postgres (design §2, §4, §7)."""

from __future__ import annotations

import asyncio

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncEngine

from quartermaster.adapters.postgres.identifiers import new_movement_id, new_reservation_id
from quartermaster.adapters.postgres.tables import movement, order_line, orders, reservation, stock
from quartermaster.adapters.postgres.unit_of_work import PostgresUnitOfWork, postgres_uow_factory
from quartermaster.application.clock import system_clock
from quartermaster.application.handlers.allocate import run_allocate
from quartermaster.application.handlers.cancel import run_cancel
from quartermaster.application.handlers.pack import run_pack
from quartermaster.application.handlers.pick import run_pick
from quartermaster.application.handlers.ship import run_ship
from quartermaster.application.ports import UnitOfWorkFactory
from quartermaster.application.results import CancelResult
from quartermaster.domain.ids import IdempotencyKey, OrderId
from quartermaster.domain.movements import MovementType
from quartermaster.domain.reservations import Reservation
from quartermaster.domain.state_machines import OrderState, ReservationState
from tests.integration.seed import assert_invariants, seed_order, seed_sku_locations_stock


def _f(engine: AsyncEngine) -> UnitOfWorkFactory:
    return postgres_uow_factory(engine)


async def _allocate(engine: AsyncEngine, oid: OrderId, key: str) -> None:
    await run_allocate(
        _f(engine),
        oid,
        IdempotencyKey(key),
        now=system_clock,
        new_reservation_id=new_reservation_id,
        new_movement_id=new_movement_id,
    )


async def _pick(engine: AsyncEngine, oid: OrderId, key: str) -> None:
    await run_pick(
        _f(engine), oid, IdempotencyKey(key), now=system_clock, new_movement_id=new_movement_id
    )


async def _cancel(engine: AsyncEngine, oid: OrderId, key: str) -> CancelResult:
    return await run_cancel(
        _f(engine), oid, IdempotencyKey(key), now=system_clock, new_movement_id=new_movement_id
    )


async def _held(engine: AsyncEngine, oid: OrderId) -> list[Reservation]:
    async with PostgresUnitOfWork(engine) as uow:
        held = await uow.reservations.held_for_order(oid)
        await uow.commit()
    return held


async def _reaper_expire(engine: AsyncEngine, res: Reservation) -> None:
    """Simulate the future reaper's per-reservation effect: held->expired + -reserved."""
    async with PostgresUnitOfWork(engine) as uow:
        if await uow.reservations.transition(
            res.reservation_id, ReservationState.HELD, ReservationState.EXPIRED
        ):
            await uow.stock.release(res.sku_id, res.location_id, res.qty)
        await uow.commit()


async def test_full_lifecycle_create_to_shipped(committed_db: AsyncEngine) -> None:
    sku = await seed_sku_locations_stock(committed_db, "S", {"L1": 5})
    order_id = await seed_order(committed_db, state=OrderState.CREATED, lines={"S": 5})
    await _allocate(committed_db, order_id, "a")
    await _pick(committed_db, order_id, "p")
    await run_pack(_f(committed_db), order_id, IdempotencyKey("k"))
    result = await run_ship(_f(committed_db), order_id, IdempotencyKey("s"))

    assert result.state is OrderState.SHIPPED
    async with committed_db.connect() as conn:
        state = (
            await conn.execute(select(orders.c.state).where(orders.c.order_id == order_id))
        ).scalar_one()
        line = (
            await conn.execute(
                select(order_line.c.picked_qty, order_line.c.shipped_qty).where(
                    order_line.c.order_id == order_id
                )
            )
        ).one()
        cell = (
            await conn.execute(
                select(stock.c.qty_on_hand, stock.c.qty_reserved).where(stock.c.sku_id == sku)
            )
        ).one()
    assert state == "shipped"
    assert (line.picked_qty, line.shipped_qty) == (5, 5)
    assert (cell.qty_on_hand, cell.qty_reserved) == (0, 0)
    await assert_invariants(committed_db, sku)


async def test_cancel_from_allocated_releases_reservations(committed_db: AsyncEngine) -> None:
    sku = await seed_sku_locations_stock(committed_db, "S", {"L1": 5})
    order_id = await seed_order(committed_db, state=OrderState.CREATED, lines={"S": 5})
    await _allocate(committed_db, order_id, "a")

    result = await _cancel(committed_db, order_id, "c")

    assert result.state is OrderState.CANCELLED
    assert len(result.released_reservation_ids) == 1
    async with committed_db.connect() as conn:
        state = (
            await conn.execute(select(orders.c.state).where(orders.c.order_id == order_id))
        ).scalar_one()
        cell = (
            await conn.execute(
                select(stock.c.qty_on_hand, stock.c.qty_reserved).where(stock.c.sku_id == sku)
            )
        ).one()
        rel = (
            await conn.execute(
                select(func.count())
                .select_from(movement)
                .where(movement.c.type == MovementType.RELEASE.value, movement.c.ref == order_id)
            )
        ).scalar_one()
        res_states = (
            (
                await conn.execute(
                    select(reservation.c.state).where(reservation.c.order_id == order_id)
                )
            )
            .scalars()
            .all()
        )
    assert state == "cancelled"
    assert (cell.qty_on_hand, cell.qty_reserved) == (5, 0)  # on_hand intact, reserved released
    assert rel == 1
    assert res_states == ["released"]
    await assert_invariants(committed_db, sku)


async def test_pick_vs_cancel_on_one_order_exactly_one_wins(committed_db: AsyncEngine) -> None:
    sku = await seed_sku_locations_stock(committed_db, "S", {"L1": 5})
    order_id = await seed_order(committed_db, state=OrderState.CREATED, lines={"S": 5})
    await _allocate(committed_db, order_id, "a")

    outcomes = await asyncio.gather(
        _pick(committed_db, order_id, "pick"),
        _cancel(committed_db, order_id, "cancel"),
        return_exceptions=True,
    )
    raised = [o for o in outcomes if isinstance(o, BaseException)]
    assert len(raised) == 1  # exactly one command lost the order-state CAS and was rejected

    async with committed_db.connect() as conn:
        state = (
            await conn.execute(select(orders.c.state).where(orders.c.order_id == order_id))
        ).scalar_one()
        res_states = (
            (
                await conn.execute(
                    select(reservation.c.state).where(reservation.c.order_id == order_id)
                )
            )
            .scalars()
            .all()
        )
        cell = (
            await conn.execute(
                select(stock.c.qty_on_hand, stock.c.qty_reserved).where(stock.c.sku_id == sku)
            )
        ).one()
    assert state in ("picked", "cancelled")
    if state == "picked":
        assert res_states == ["consumed"] and (cell.qty_on_hand, cell.qty_reserved) == (0, 0)
    else:
        assert res_states == ["released"] and (cell.qty_on_hand, cell.qty_reserved) == (5, 0)
    await assert_invariants(committed_db, sku)  # never both: consumed XOR released


async def test_cancel_races_simulated_reaper_expiry_frees_once(committed_db: AsyncEngine) -> None:
    sku = await seed_sku_locations_stock(committed_db, "S", {"L1": 5})
    order_id = await seed_order(committed_db, state=OrderState.CREATED, lines={"S": 5})
    await _allocate(committed_db, order_id, "a")
    (res,) = await _held(committed_db, order_id)

    await asyncio.gather(_cancel(committed_db, order_id, "c"), _reaper_expire(committed_db, res))

    async with committed_db.connect() as conn:
        state = (
            await conn.execute(select(orders.c.state).where(orders.c.order_id == order_id))
        ).scalar_one()
        cell = (
            await conn.execute(
                select(stock.c.qty_on_hand, stock.c.qty_reserved).where(stock.c.sku_id == sku)
            )
        ).one()
        res_state = (
            await conn.execute(
                select(reservation.c.state).where(
                    reservation.c.reservation_id == res.reservation_id
                )
            )
        ).scalar_one()
    assert state == "cancelled"  # cancel's order CAS is uncontended by the reaper-sim
    assert (cell.qty_on_hand, cell.qty_reserved) == (
        5,
        0,
    )  # reserved freed exactly once, never negative
    assert res_state in ("released", "expired")  # whichever actor won the reservation CAS
    await assert_invariants(committed_db, sku)
