"""The three decrement strategies under a hot-cell race (design spec §7).

Each test starts from a clean store: ``committed_db`` truncates on teardown, so
separate test functions never see each other's writes (no inline TRUNCATE needed).
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Callable

from loadtest.strategies import cas_uow_factory, guarded_uow_factory, naive_uow_factory
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncEngine

from quartermaster.adapters.postgres.identifiers import new_movement_id, new_reservation_id
from quartermaster.adapters.postgres.tables import reservation, stock
from quartermaster.application.clock import system_clock
from quartermaster.application.handlers.allocate import run_allocate
from quartermaster.application.ports import UnitOfWorkFactory
from quartermaster.domain.ids import IdempotencyKey, OrderId
from quartermaster.domain.state_machines import OrderState, ReservationState
from tests.integration.seed import seed_order, seed_sku_locations_stock


async def _storm(
    engine: AsyncEngine,
    factory_builder: Callable[[AsyncEngine], UnitOfWorkFactory],
    key_prefix: str,
) -> None:
    # One hot cell with 4 on hand; 10 orders each want 1.
    await seed_sku_locations_stock(engine, "S", {"L1": 4})
    order_ids = [
        await seed_order(engine, state=OrderState.CREATED, lines={"S": 1}) for _ in range(10)
    ]
    factory = factory_builder(engine)

    async def run(i: int, oid: OrderId) -> None:
        # Strategies may raise OccConflict/RetryExhausted under the race; swallow
        # so the storm completes and we can audit the final state.
        with contextlib.suppress(Exception):
            await run_allocate(
                factory,
                oid,
                IdempotencyKey(f"{key_prefix}-{i}"),
                now=system_clock,
                new_reservation_id=new_reservation_id,
                new_movement_id=new_movement_id,
            )

    await asyncio.gather(*(run(i, oid) for i, oid in enumerate(order_ids)))


async def _reserved_vs_ledger(engine: AsyncEngine) -> tuple[int, int]:
    async with engine.connect() as conn:
        stock_reserved = (
            await conn.execute(select(func.coalesce(func.sum(stock.c.qty_reserved), 0)))
        ).scalar_one()
        held = (
            await conn.execute(
                select(func.coalesce(func.sum(reservation.c.qty), 0)).where(
                    reservation.c.state == ReservationState.HELD.value
                )
            )
        ).scalar_one()
    return int(stock_reserved), int(held)


async def test_naive_oversells(committed_db: AsyncEngine) -> None:
    await _storm(committed_db, naive_uow_factory, "naive")
    stock_reserved, held = await _reserved_vs_ledger(committed_db)
    # The lost update is silent at the storage layer (all CHECKs satisfied) yet
    # the held reservations exceed what the stock row reflects: a torn write.
    assert stock_reserved < held


async def test_cas_never_oversells(committed_db: AsyncEngine) -> None:
    await _storm(committed_db, cas_uow_factory, "cas")
    stock_reserved, held = await _reserved_vs_ledger(committed_db)
    assert stock_reserved == held
    assert stock_reserved <= 4  # never more than on-hand


async def test_guarded_never_oversells(committed_db: AsyncEngine) -> None:
    await _storm(committed_db, guarded_uow_factory, "grd")
    stock_reserved, held = await _reserved_vs_ledger(committed_db)
    assert stock_reserved == held
    assert stock_reserved <= 4
