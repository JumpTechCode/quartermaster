# tests/integration/test_postgres_repos.py
"""The Postgres repositories implement the ports against a real database."""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine

from quartermaster.adapters.postgres.identifiers import new_order_id
from quartermaster.adapters.postgres.tables import location, orders, sku, stock
from quartermaster.adapters.postgres.unit_of_work import PostgresUnitOfWork
from quartermaster.application.ports import ClaimOutcome
from quartermaster.domain.idempotency import IdempotencyStatus
from quartermaster.domain.ids import IdempotencyKey, LocationId, SkuId
from quartermaster.domain.state_machines import OrderState


async def _seed_two_cells(engine: AsyncEngine, on_hand: int) -> SkuId:
    async with engine.begin() as conn:
        await conn.execute(sku.insert().values(sku_id="S", description="w", unit="each"))
        await conn.execute(location.insert().values(location_id="L1", kind="shelf"))
        await conn.execute(location.insert().values(location_id="L2", kind="shelf"))
        await conn.execute(
            stock.insert().values(sku_id="S", location_id="L1", qty_on_hand=on_hand, qty_reserved=0)
        )
        await conn.execute(
            stock.insert().values(sku_id="S", location_id="L2", qty_on_hand=on_hand, qty_reserved=0)
        )
    return SkuId("S")


async def test_reserve_up_to_is_atomic_partial(committed_db: AsyncEngine) -> None:
    sku_id = await _seed_two_cells(committed_db, on_hand=3)
    async with PostgresUnitOfWork(committed_db) as uow:
        assert await uow.stock.reserve_up_to(sku_id, LocationId("L1"), 5) == 3  # only 3 available
        assert await uow.stock.reserve_up_to(sku_id, LocationId("L1"), 5) == 0  # now exhausted
        await uow.commit()
    async with committed_db.connect() as conn:
        row = (
            await conn.execute(select(stock.c.qty_reserved).where(stock.c.location_id == "L1"))
        ).one()
        assert row.qty_reserved == 3


async def test_stock_locations_orders_and_filters(committed_db: AsyncEngine) -> None:
    sku_id = await _seed_two_cells(committed_db, on_hand=2)
    async with PostgresUnitOfWork(committed_db) as uow:
        await uow.stock.reserve_up_to(sku_id, LocationId("L1"), 2)  # drain L1 to 0 available
        locs = await uow.stock.stock_locations(sku_id)
        await uow.commit()
    assert locs == [(LocationId("L2"), 2)]  # L1 filtered (available 0); ordered by id


async def test_cas_state_succeeds_then_conflicts(committed_db: AsyncEngine) -> None:
    order_id = new_order_id()
    async with committed_db.begin() as conn:
        await conn.execute(
            orders.insert().values(
                order_id=order_id, state="created", version=1, created_at=datetime.now(UTC)
            )
        )
    async with PostgresUnitOfWork(committed_db) as uow:
        assert (
            await uow.orders.cas_state(order_id, OrderState.CREATED, 1, OrderState.ALLOCATED)
            is True
        )
        await uow.commit()
    async with PostgresUnitOfWork(committed_db) as uow:
        # stale expected version/state now -> 0 rows
        assert (
            await uow.orders.cas_state(order_id, OrderState.CREATED, 1, OrderState.ALLOCATED)
            is False
        )
        await uow.commit()


async def test_idempotency_claim_load_finalize(committed_db: AsyncEngine) -> None:
    key = IdempotencyKey("k1")
    async with PostgresUnitOfWork(committed_db) as uow:
        assert await uow.idempotency.claim(key, "fp") is ClaimOutcome.CLAIMED
        await uow.idempotency.finalize(key, IdempotencyStatus.SUCCEEDED, {"value": 1})
        await uow.commit()
    async with PostgresUnitOfWork(committed_db) as uow:
        assert await uow.idempotency.claim(key, "fp") is ClaimOutcome.EXISTS  # already present
        stored = await uow.idempotency.load(key)
        await uow.commit()
    assert stored is not None
    assert stored.status is IdempotencyStatus.SUCCEEDED
    assert stored.response == {"value": 1}
    assert stored.command_fingerprint == "fp"


async def test_rollback_discards_writes(committed_db: AsyncEngine) -> None:
    key = IdempotencyKey("rollback-key")
    async with PostgresUnitOfWork(committed_db) as uow:
        await uow.idempotency.claim(key, "fp")
        await uow.rollback()
    async with PostgresUnitOfWork(committed_db) as uow:
        assert await uow.idempotency.load(key) is None  # claim was rolled back
        await uow.commit()
