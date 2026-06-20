"""Behavioural + concurrency tests of the receive flow on real Postgres (design §7)."""

from __future__ import annotations

import asyncio

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncEngine

from quartermaster.adapters.postgres.identifiers import new_movement_id, new_receipt_id
from quartermaster.adapters.postgres.tables import movement, receipt, receipt_line, stock
from quartermaster.adapters.postgres.unit_of_work import postgres_uow_factory
from quartermaster.application.clock import system_clock
from quartermaster.application.handlers.arrive import run_arrive
from quartermaster.application.handlers.create_receipt import run_create_receipt
from quartermaster.application.handlers.receive import run_receive
from quartermaster.application.results import CreateReceiptResult, ReceiveResult
from quartermaster.domain.errors import IllegalTransition
from quartermaster.domain.ids import IdempotencyKey, LocationId, ReceiptId, SkuId
from quartermaster.domain.movements import MovementType
from quartermaster.domain.state_machines import ReceiptState
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


async def _arrive(engine: AsyncEngine, receipt_id: ReceiptId, key: str) -> None:
    await run_arrive(postgres_uow_factory(engine), receipt_id, IdempotencyKey(key))


async def _receive(
    engine: AsyncEngine,
    receipt_id: ReceiptId,
    location: LocationId,
    lines: tuple[tuple[SkuId, int], ...],
    key: str,
) -> ReceiveResult:
    return await run_receive(
        postgres_uow_factory(engine),
        receipt_id,
        location,
        lines,
        IdempotencyKey(key),
        now=system_clock,
        new_movement_id=new_movement_id,
    )


async def test_receive_full_lifecycle_lands_stock(committed_db: AsyncEngine) -> None:
    await seed_sku(committed_db, "S")
    await seed_location(committed_db, "RCV", "receiving")
    created = await _create(committed_db, ((SkuId("S"), 5),), "create")
    await _arrive(committed_db, created.receipt_id, "arrive")

    result = await _receive(
        committed_db, created.receipt_id, LocationId("RCV"), ((SkuId("S"), 5),), "recv"
    )

    assert result.state is ReceiptState.RECEIVED
    assert [(line.sku_id, line.received) for line in result.lines] == [("S", 5)]
    async with committed_db.connect() as conn:
        header = (
            await conn.execute(
                select(receipt.c.state).where(receipt.c.receipt_id == created.receipt_id)
            )
        ).scalar_one()
        line = (
            await conn.execute(
                select(receipt_line.c.received_qty, receipt_line.c.expected_qty).where(
                    receipt_line.c.receipt_id == created.receipt_id
                )
            )
        ).one()
        cell = (
            await conn.execute(
                select(stock.c.qty_on_hand, stock.c.qty_reserved).where(stock.c.sku_id == "S")
            )
        ).one()
        receives = (
            await conn.execute(
                select(func.count())
                .select_from(movement)
                .where(
                    movement.c.type == MovementType.RECEIVE.value,
                    movement.c.ref == created.receipt_id,
                )
            )
        ).scalar_one()
        landed = (
            await conn.execute(
                select(func.coalesce(func.sum(movement.c.qty), 0)).where(
                    movement.c.type == MovementType.RECEIVE.value, movement.c.sku_id == "S"
                )
            )
        ).scalar_one()
    assert header == "received"
    assert (line.received_qty, line.expected_qty) == (5, 5)
    assert (cell.qty_on_hand, cell.qty_reserved) == (5, 0)
    assert receives == 1
    assert landed == cell.qty_on_hand  # conservation: Σ RECEIVE movements == on_hand


async def test_receive_partial_records_shortfall(committed_db: AsyncEngine) -> None:
    await seed_sku(committed_db, "S")
    await seed_location(committed_db, "RCV", "receiving")
    created = await _create(committed_db, ((SkuId("S"), 10),), "c")
    await _arrive(committed_db, created.receipt_id, "a")

    result = await _receive(
        committed_db, created.receipt_id, LocationId("RCV"), ((SkuId("S"), 4),), "r"
    )

    assert result.state is ReceiptState.RECEIVED
    async with committed_db.connect() as conn:
        line = (
            await conn.execute(
                select(receipt_line.c.received_qty, receipt_line.c.expected_qty).where(
                    receipt_line.c.receipt_id == created.receipt_id
                )
            )
        ).one()
        cell = (
            await conn.execute(select(stock.c.qty_on_hand).where(stock.c.sku_id == "S"))
        ).scalar_one()
    assert (line.received_qty, line.expected_qty) == (4, 10)  # shortfall recorded
    assert cell == 4


async def test_same_key_receive_fired_concurrently_is_one_effect(committed_db: AsyncEngine) -> None:
    await seed_sku(committed_db, "S")
    await seed_location(committed_db, "RCV", "receiving")
    created = await _create(committed_db, ((SkuId("S"), 5),), "c")
    await _arrive(committed_db, created.receipt_id, "a")

    results = await asyncio.gather(
        *(
            _receive(
                committed_db, created.receipt_id, LocationId("RCV"), ((SkuId("S"), 5),), "same"
            )
            for _ in range(6)
        )
    )

    assert all(r == results[0] for r in results)  # one effect, replayed
    async with committed_db.connect() as conn:
        receives = (
            await conn.execute(
                select(func.count())
                .select_from(movement)
                .where(
                    movement.c.ref == created.receipt_id,
                    movement.c.type == MovementType.RECEIVE.value,
                )
            )
        ).scalar_one()
        cell = (
            await conn.execute(select(stock.c.qty_on_hand).where(stock.c.sku_id == "S"))
        ).scalar_one()
    assert receives == 1  # exactly one consume despite 6 concurrent calls
    assert cell == 5


async def test_distinct_key_receives_one_wins_one_rejected(committed_db: AsyncEngine) -> None:
    await seed_sku(committed_db, "S")
    await seed_location(committed_db, "RCV", "receiving")
    created = await _create(committed_db, ((SkuId("S"), 5),), "c")
    await _arrive(committed_db, created.receipt_id, "a")

    results = await asyncio.gather(
        _receive(committed_db, created.receipt_id, LocationId("RCV"), ((SkuId("S"), 5),), "k1"),
        _receive(committed_db, created.receipt_id, LocationId("RCV"), ((SkuId("S"), 5),), "k2"),
        return_exceptions=True,
    )

    succeeded = [r for r in results if isinstance(r, ReceiveResult)]
    rejected = [r for r in results if isinstance(r, IllegalTransition)]
    assert len(succeeded) == 1  # only one transition out of `arrived`
    assert len(rejected) == 1  # the loser reloads `received` and is rejected
    async with committed_db.connect() as conn:
        cell = (
            await conn.execute(select(stock.c.qty_on_hand).where(stock.c.sku_id == "S"))
        ).scalar_one()
    assert cell == 5  # stock landed exactly once
