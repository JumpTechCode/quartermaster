"""Behavioural + concurrency tests of the putaway flow on real Postgres (design §7)."""

from __future__ import annotations

import asyncio

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncEngine

from quartermaster.adapters.postgres.identifiers import (
    new_movement_id,
    new_order_id,
    new_receipt_id,
    new_reservation_id,
)
from quartermaster.adapters.postgres.tables import movement, receipt, reservation, stock
from quartermaster.adapters.postgres.unit_of_work import postgres_uow_factory
from quartermaster.application.clock import system_clock
from quartermaster.application.handlers.allocate import run_allocate
from quartermaster.application.handlers.arrive import run_arrive
from quartermaster.application.handlers.cancel_receipt import run_cancel_receipt
from quartermaster.application.handlers.close_receipt import run_close_receipt
from quartermaster.application.handlers.create_order import run_create_order
from quartermaster.application.handlers.create_receipt import run_create_receipt
from quartermaster.application.handlers.putaway import run_putaway
from quartermaster.application.handlers.receive import run_receive
from quartermaster.application.results import CreateReceiptResult, PutawayResult
from quartermaster.domain.errors import IllegalTransition
from quartermaster.domain.ids import IdempotencyKey, LocationId, ReceiptId, SkuId
from quartermaster.domain.movements import MovementType
from quartermaster.domain.state_machines import OrderState, ReceiptState
from tests.integration.seed import seed_location, seed_sku


async def _create(
    engine: AsyncEngine, lines: tuple[tuple[SkuId, int], ...], key: str
) -> CreateReceiptResult:
    return await run_create_receipt(
        postgres_uow_factory(engine),
        lines,
        IdempotencyKey(key),
        now=system_clock,
        new_receipt_id=new_receipt_id,
    )


async def _arrive(engine: AsyncEngine, rid: ReceiptId, key: str) -> None:
    await run_arrive(postgres_uow_factory(engine), rid, IdempotencyKey(key))


async def _receive(
    engine: AsyncEngine,
    rid: ReceiptId,
    loc: LocationId,
    lines: tuple[tuple[SkuId, int], ...],
    key: str,
) -> None:
    await run_receive(
        postgres_uow_factory(engine),
        rid,
        loc,
        lines,
        IdempotencyKey(key),
        now=system_clock,
        new_movement_id=new_movement_id,
    )


async def _putaway(
    engine: AsyncEngine, rid: ReceiptId, frm: LocationId, to: LocationId, key: str
) -> PutawayResult:
    return await run_putaway(
        postgres_uow_factory(engine),
        rid,
        frm,
        to,
        IdempotencyKey(key),
        now=system_clock,
        new_movement_id=new_movement_id,
    )


async def _close(engine: AsyncEngine, rid: ReceiptId, key: str) -> None:
    await run_close_receipt(postgres_uow_factory(engine), rid, IdempotencyKey(key))


async def _receive_ready(engine: AsyncEngine, sku: str, qty: int, key: str) -> ReceiptId:
    """create -> arrive -> receive `qty` of `sku` at RCV; return a receipt in `received`."""
    created = await _create(engine, ((SkuId(sku), qty),), f"{key}-c")
    await _arrive(engine, created.receipt_id, f"{key}-a")
    await _receive(engine, created.receipt_id, LocationId("RCV"), ((SkuId(sku), qty),), f"{key}-r")
    return created.receipt_id


async def test_putaway_full_lifecycle_relocates_and_closes(committed_db: AsyncEngine) -> None:
    await seed_sku(committed_db, "S")
    await seed_location(committed_db, "RCV", "receiving")
    await seed_location(committed_db, "A1", "shelf")
    rid = await _receive_ready(committed_db, "S", 5, "k")

    result = await _putaway(committed_db, rid, LocationId("RCV"), LocationId("A1"), "put")
    assert result.state is ReceiptState.PUTAWAY_COMPLETE
    assert [(line.sku_id, line.moved) for line in result.lines] == [("S", 5)]
    await _close(committed_db, rid, "close")

    async with committed_db.connect() as conn:
        state = (
            await conn.execute(
                select(stock.c.qty_on_hand, stock.c.location_id).where(stock.c.sku_id == "S")
            )
        ).all()
        on_hand = {r.location_id: r.qty_on_hand for r in state}
        receives = (
            await conn.execute(
                select(func.coalesce(func.sum(movement.c.qty), 0)).where(
                    movement.c.type == MovementType.RECEIVE.value, movement.c.sku_id == "S"
                )
            )
        ).scalar_one()
        putaways = (
            await conn.execute(
                select(func.count())
                .select_from(movement)
                .where(movement.c.type == MovementType.PUTAWAY.value, movement.c.ref == rid)
            )
        ).scalar_one()
    assert on_hand.get("RCV", 0) == 0  # left the dock
    assert on_hand.get("A1") == 5  # landed on the shelf
    assert putaways == 1
    # conservation: PUTAWAY is net-zero, so total on-hand == Σ RECEIVE
    assert sum(on_hand.values()) == receives == 5


async def test_putaway_partial_received_relocates_actual(committed_db: AsyncEngine) -> None:
    await seed_sku(committed_db, "S")
    await seed_location(committed_db, "RCV", "receiving")
    await seed_location(committed_db, "A1", "shelf")
    created = await _create(committed_db, ((SkuId("S"), 10),), "c")
    await _arrive(committed_db, created.receipt_id, "a")
    await _receive(
        committed_db, created.receipt_id, LocationId("RCV"), ((SkuId("S"), 4),), "r"
    )  # short

    await _putaway(committed_db, created.receipt_id, LocationId("RCV"), LocationId("A1"), "put")

    async with committed_db.connect() as conn:
        a1 = (
            await conn.execute(select(stock.c.qty_on_hand).where(stock.c.location_id == "A1"))
        ).scalar_one()
        rcv = (
            await conn.execute(select(stock.c.qty_on_hand).where(stock.c.location_id == "RCV"))
        ).scalar_one()
    assert (rcv, a1) == (0, 4)


async def test_same_key_putaway_is_one_effect(committed_db: AsyncEngine) -> None:
    await seed_sku(committed_db, "S")
    await seed_location(committed_db, "RCV", "receiving")
    await seed_location(committed_db, "A1", "shelf")
    rid = await _receive_ready(committed_db, "S", 5, "k")

    results = await asyncio.gather(
        *(
            _putaway(committed_db, rid, LocationId("RCV"), LocationId("A1"), "same")
            for _ in range(6)
        )
    )

    assert all(r == results[0] for r in results)
    async with committed_db.connect() as conn:
        putaways = (
            await conn.execute(
                select(func.count())
                .select_from(movement)
                .where(movement.c.ref == rid, movement.c.type == MovementType.PUTAWAY.value)
            )
        ).scalar_one()
        a1 = (
            await conn.execute(select(stock.c.qty_on_hand).where(stock.c.location_id == "A1"))
        ).scalar_one()
    assert putaways == 1
    assert a1 == 5


async def test_distinct_key_putaway_one_wins_one_rejected(committed_db: AsyncEngine) -> None:
    await seed_sku(committed_db, "S")
    await seed_location(committed_db, "RCV", "receiving")
    await seed_location(committed_db, "A1", "shelf")
    rid = await _receive_ready(committed_db, "S", 5, "k")

    results = await asyncio.gather(
        _putaway(committed_db, rid, LocationId("RCV"), LocationId("A1"), "k1"),
        _putaway(committed_db, rid, LocationId("RCV"), LocationId("A1"), "k2"),
        return_exceptions=True,
    )
    succeeded = [r for r in results if isinstance(r, PutawayResult)]
    rejected = [r for r in results if isinstance(r, IllegalTransition)]
    assert len(succeeded) == 1
    assert len(rejected) == 1
    async with committed_db.connect() as conn:
        a1 = (
            await conn.execute(select(stock.c.qty_on_hand).where(stock.c.location_id == "A1"))
        ).scalar_one()
    assert a1 == 5  # relocated exactly once


async def test_cancel_from_arrived(committed_db: AsyncEngine) -> None:
    await seed_sku(committed_db, "S")
    created = await _create(committed_db, ((SkuId("S"), 5),), "c")
    await _arrive(committed_db, created.receipt_id, "a")

    result = await run_cancel_receipt(
        postgres_uow_factory(committed_db), created.receipt_id, IdempotencyKey("cancel")
    )
    assert result.state is ReceiptState.CANCELLED
    async with committed_db.connect() as conn:
        state = (
            await conn.execute(
                select(receipt.c.state).where(receipt.c.receipt_id == created.receipt_id)
            )
        ).scalar_one()
    assert state == "cancelled"


async def test_allocate_ignores_dock_stock_until_putaway(committed_db: AsyncEngine) -> None:
    await seed_sku(committed_db, "S")
    await seed_location(committed_db, "RCV", "receiving")
    await seed_location(committed_db, "A1", "shelf")
    rid = await _receive_ready(committed_db, "S", 5, "k")  # 5 of S on the dock (RCV)

    order = await run_create_order(
        postgres_uow_factory(committed_db),
        ((SkuId("S"), 5),),
        IdempotencyKey("o-create"),
        now=system_clock,
        new_order_id=new_order_id,
    )

    async def _allocate(key: str) -> OrderState:
        result = await run_allocate(
            postgres_uow_factory(committed_db),
            order.order_id,
            IdempotencyKey(key),
            now=system_clock,
            new_reservation_id=new_reservation_id,
            new_movement_id=new_movement_id,
        )
        return result.state

    # dock stock is not allocatable -> the order backorders, nothing reserved at RCV
    assert await _allocate("alloc-1") is OrderState.BACKORDERED
    async with committed_db.connect() as conn:
        rcv_reserved = (
            await conn.execute(select(stock.c.qty_reserved).where(stock.c.location_id == "RCV"))
        ).scalar_one()
        held = (
            await conn.execute(
                select(func.count()).select_from(reservation).where(reservation.c.sku_id == "S")
            )
        ).scalar_one()
    assert rcv_reserved == 0
    assert held == 0

    # putaway moves it to a shelf; now the same order allocates
    await _putaway(committed_db, rid, LocationId("RCV"), LocationId("A1"), "put")
    assert await _allocate("alloc-2") is OrderState.ALLOCATED
    async with committed_db.connect() as conn:
        a1_reserved = (
            await conn.execute(select(stock.c.qty_reserved).where(stock.c.location_id == "A1"))
        ).scalar_one()
    assert a1_reserved == 5
