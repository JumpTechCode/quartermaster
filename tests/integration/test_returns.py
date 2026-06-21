"""End-to-end customer-RMA (return) flow on real Postgres (design §2, ADR-0008)."""

from __future__ import annotations

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine

from quartermaster.adapters.postgres.identifiers import (
    new_movement_id,
    new_order_id,
    new_receipt_id,
)
from quartermaster.adapters.postgres.tables import receipt, stock
from quartermaster.adapters.postgres.unit_of_work import postgres_uow_factory
from quartermaster.application.clock import system_clock
from quartermaster.application.handlers.arrive import run_arrive
from quartermaster.application.handlers.create_return import run_create_return
from quartermaster.application.handlers.putaway import run_putaway
from quartermaster.application.handlers.receive import run_receive
from quartermaster.domain.errors import OrderNotFound, ReturnNotAllowed
from quartermaster.domain.ids import IdempotencyKey, LocationId, OrderId, SkuId
from quartermaster.domain.receipts import ReceiptKind
from quartermaster.domain.state_machines import OrderState, ReceiptState
from tests.integration.seed import seed_location, seed_order, seed_sku


async def _ship_order(engine: AsyncEngine, sku: str, qty: int) -> OrderId:
    await seed_sku(engine, sku)
    return await seed_order(
        engine,
        state=OrderState.SHIPPED,
        lines={sku: qty},
        allocated={sku: qty},
        picked={sku: qty},
        shipped={sku: qty},
    )


async def test_return_flow_lands_stock_back_on_shelf(committed_db: AsyncEngine) -> None:
    order_id = await _ship_order(committed_db, "S", 5)
    await seed_location(committed_db, "RCV", "receiving")
    await seed_location(committed_db, "A1", "shelf")
    factory = postgres_uow_factory(committed_db)

    created = await run_create_return(
        factory,
        order_id,
        ((SkuId("S"), 3),),
        IdempotencyKey("ret"),
        now=system_clock,
        new_receipt_id=new_receipt_id,
    )
    assert created.kind is ReceiptKind.CUSTOMER_RMA
    assert created.state is ReceiptState.EXPECTED

    await run_arrive(factory, created.receipt_id, IdempotencyKey("arr"))
    await run_receive(
        factory,
        created.receipt_id,
        LocationId("RCV"),
        ((SkuId("S"), 3),),
        IdempotencyKey("rcv"),
        now=system_clock,
        new_movement_id=new_movement_id,
    )
    await run_putaway(
        factory,
        created.receipt_id,
        LocationId("RCV"),
        LocationId("A1"),
        IdempotencyKey("put"),
        now=system_clock,
        new_movement_id=new_movement_id,
    )

    async with committed_db.connect() as conn:
        hdr = (
            await conn.execute(
                select(receipt.c.kind, receipt.c.origin_order_id, receipt.c.state).where(
                    receipt.c.receipt_id == created.receipt_id
                )
            )
        ).one()
        shelf = (
            await conn.execute(
                select(stock.c.qty_on_hand).where(
                    stock.c.sku_id == "S", stock.c.location_id == "A1"
                )
            )
        ).scalar_one()
    assert hdr.kind == "customer_rma"
    assert hdr.origin_order_id == order_id
    assert hdr.state == "putaway_complete"
    assert shelf == 3  # returned stock is back on the shelf via the inbound path


async def test_return_unknown_order_rejected(committed_db: AsyncEngine) -> None:
    with pytest.raises(OrderNotFound):
        await run_create_return(
            postgres_uow_factory(committed_db),
            new_order_id(),
            ((SkuId("S"), 1),),
            IdempotencyKey("k"),
            now=system_clock,
            new_receipt_id=new_receipt_id,
        )


async def test_return_unshipped_order_rejected(committed_db: AsyncEngine) -> None:
    await seed_sku(committed_db, "S")
    order_id = await seed_order(
        committed_db,
        state=OrderState.ALLOCATED,
        lines={"S": 5},
        allocated={"S": 5},
    )
    with pytest.raises(ReturnNotAllowed):
        await run_create_return(
            postgres_uow_factory(committed_db),
            order_id,
            ((SkuId("S"), 1),),
            IdempotencyKey("k"),
            now=system_clock,
            new_receipt_id=new_receipt_id,
        )


async def test_return_over_shipped_qty_rejected(committed_db: AsyncEngine) -> None:
    order_id = await _ship_order(committed_db, "S", 5)
    with pytest.raises(ReturnNotAllowed):
        await run_create_return(
            postgres_uow_factory(committed_db),
            order_id,
            ((SkuId("S"), 6),),
            IdempotencyKey("k"),
            now=system_clock,
            new_receipt_id=new_receipt_id,
        )
