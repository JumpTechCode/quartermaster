"""Reaper behaviour and races on real Postgres (design §5.4, §5.5, §7)."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncEngine

from quartermaster.adapters.postgres.identifiers import new_movement_id
from quartermaster.adapters.postgres.tables import idempotency_key, movement, reservation, stock
from quartermaster.adapters.postgres.unit_of_work import postgres_uow_factory
from quartermaster.application.clock import system_clock
from quartermaster.application.handlers.cancel import run_cancel
from quartermaster.domain.ids import IdempotencyKey
from quartermaster.domain.state_machines import OrderState
from quartermaster.workers.idempotency_reaper import reap_idempotency_keys
from quartermaster.workers.reservation_reaper import reap_reservations
from tests.integration.seed import (
    assert_invariants,
    seed_held_reservation,
    seed_order,
    seed_sku_locations_stock,
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


async def _movements(engine: AsyncEngine, sku: str, mv_type: str) -> int:
    async with engine.connect() as conn:
        return len(
            (
                await conn.execute(
                    select(movement.c.movement_id).where(
                        movement.c.sku_id == sku, movement.c.type == mv_type
                    )
                )
            ).all()
        )


async def _reservation_state(engine: AsyncEngine, reservation_id: object) -> str:
    async with engine.connect() as conn:
        return str(
            (
                await conn.execute(
                    select(reservation.c.state).where(
                        reservation.c.reservation_id == reservation_id
                    )
                )
            ).scalar_one()
        )


async def test_expires_due_reservation(committed_db: AsyncEngine) -> None:
    sku = await seed_sku_locations_stock(committed_db, "S", {"L1": 10})
    order_id = await seed_order(committed_db, state=OrderState.ALLOCATED, lines={"S": 3})
    res_id = await seed_held_reservation(
        committed_db,
        sku=sku,
        location="L1",
        order_id=order_id,
        qty=3,
        expires_at=datetime.now(UTC) - timedelta(minutes=20),
    )

    run = await reap_reservations(
        postgres_uow_factory(committed_db),
        now=system_clock,
        new_movement_id=new_movement_id,
        batch_size=500,
    )

    assert run.scanned == 1 and run.acted == 1 and run.errors == 0
    assert await _reservation_state(committed_db, res_id) == "expired"
    assert await _reserved(committed_db, sku) == 0  # released exactly once
    assert await _movements(committed_db, sku, "expire") == 1
    await assert_invariants(committed_db, sku)


async def test_not_yet_due_reservation_is_untouched(committed_db: AsyncEngine) -> None:
    sku = await seed_sku_locations_stock(committed_db, "S", {"L1": 10})
    order_id = await seed_order(committed_db, state=OrderState.ALLOCATED, lines={"S": 3})
    res_id = await seed_held_reservation(
        committed_db,
        sku=sku,
        location="L1",
        order_id=order_id,
        qty=3,
        expires_at=datetime.now(UTC) + timedelta(minutes=20),
    )

    run = await reap_reservations(
        postgres_uow_factory(committed_db),
        now=system_clock,
        new_movement_id=new_movement_id,
        batch_size=500,
    )

    assert run.scanned == 0 and run.acted == 0
    assert await _reservation_state(committed_db, res_id) == "held"
    assert await _reserved(committed_db, sku) == 3
    await assert_invariants(committed_db, sku)


async def test_concurrent_reapers_expire_exactly_once(committed_db: AsyncEngine) -> None:
    sku = await seed_sku_locations_stock(committed_db, "S", {"L1": 10})
    order_id = await seed_order(committed_db, state=OrderState.ALLOCATED, lines={"S": 3})
    await seed_held_reservation(
        committed_db,
        sku=sku,
        location="L1",
        order_id=order_id,
        qty=3,
        expires_at=datetime.now(UTC) - timedelta(minutes=20),
    )

    factory = postgres_uow_factory(committed_db)

    async def pass_() -> int:
        run = await reap_reservations(
            factory, now=system_clock, new_movement_id=new_movement_id, batch_size=500
        )
        return run.acted

    # Two passes race under one event loop. Depending on I/O interleaving the
    # loser hits either the HELD->EXPIRED CAS (0 rows) or the scan-skip path;
    # both are correct and the assertions below hold for either (cf. test_allocate_races).
    acted = await asyncio.gather(pass_(), pass_())

    assert sum(acted) == 1  # the CAS guard: exactly one pass expired it
    assert await _reserved(committed_db, sku) == 0  # lowered exactly once, never negative
    assert await _movements(committed_db, sku, "expire") == 1
    await assert_invariants(committed_db, sku)


async def test_reaper_versus_cancel_one_effect(committed_db: AsyncEngine) -> None:
    sku = await seed_sku_locations_stock(committed_db, "S", {"L1": 10})
    order_id = await seed_order(committed_db, state=OrderState.ALLOCATED, lines={"S": 3})
    res_id = await seed_held_reservation(
        committed_db,
        sku=sku,
        location="L1",
        order_id=order_id,
        qty=3,
        expires_at=datetime.now(UTC) - timedelta(minutes=20),
    )
    factory = postgres_uow_factory(committed_db)

    async def reap() -> None:
        await reap_reservations(
            factory, now=system_clock, new_movement_id=new_movement_id, batch_size=500
        )

    async def cancel() -> None:
        await run_cancel(
            factory,
            order_id,
            IdempotencyKey("cancel-key"),
            now=system_clock,
            new_movement_id=new_movement_id,
        )

    await asyncio.gather(reap(), cancel(), return_exceptions=True)

    assert await _reservation_state(committed_db, res_id) in {"expired", "released"}
    assert await _reserved(committed_db, sku) == 0  # exactly one of the two released the stock
    total_release_like = await _movements(committed_db, sku, "expire") + await _movements(
        committed_db, sku, "release"
    )
    assert total_release_like == 1
    await assert_invariants(committed_db, sku)


async def test_idempotency_reaper_deletes_only_past_cutoff(committed_db: AsyncEngine) -> None:
    now = datetime.now(UTC)
    async with committed_db.begin() as conn:
        await conn.execute(
            idempotency_key.insert(),
            [
                {
                    "key": "old-1",
                    "command_fingerprint": "fp",
                    "status": "succeeded",
                    "response": None,
                    "created_at": now - timedelta(days=2),
                },
                {
                    "key": "old-2",
                    "command_fingerprint": "fp",
                    "status": "succeeded",
                    "response": None,
                    "created_at": now - timedelta(days=3),
                },
                {
                    "key": "fresh",
                    "command_fingerprint": "fp",
                    "status": "succeeded",
                    "response": None,
                    "created_at": now - timedelta(hours=1),
                },
            ],
        )

    run = await reap_idempotency_keys(
        postgres_uow_factory(committed_db),
        now=system_clock,
        ttl=timedelta(hours=24),
        batch_size=500,
    )

    assert run.acted == 2
    async with committed_db.connect() as conn:
        keys = {r.key for r in (await conn.execute(select(idempotency_key.c.key))).all()}
    assert keys == {"fresh"}


async def test_idempotency_reaper_respects_batch_limit(committed_db: AsyncEngine) -> None:
    now = datetime.now(UTC)
    async with committed_db.begin() as conn:
        await conn.execute(
            idempotency_key.insert(),
            [
                {
                    "key": f"old-{i}",
                    "command_fingerprint": "fp",
                    "status": "succeeded",
                    "response": None,
                    "created_at": now - timedelta(days=2),
                }
                for i in range(5)
            ],
        )

    run = await reap_idempotency_keys(
        postgres_uow_factory(committed_db),
        now=system_clock,
        ttl=timedelta(hours=24),
        batch_size=2,
    )

    assert run.acted == 5  # drains across 2 + 2 + 1 across three bounded batches
    async with committed_db.connect() as conn:
        remaining = (
            await conn.execute(select(func.count()).select_from(idempotency_key))
        ).scalar_one()
    assert remaining == 0
