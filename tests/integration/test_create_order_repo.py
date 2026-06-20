"""Behavioral tests of the create_order command on real Postgres."""

from __future__ import annotations

from collections.abc import Callable, Coroutine

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine

from quartermaster.adapters.postgres.identifiers import new_order_id
from quartermaster.adapters.postgres.tables import order_line, orders
from quartermaster.adapters.postgres.unit_of_work import postgres_uow_factory
from quartermaster.application.clock import system_clock
from quartermaster.application.handlers.create_order import run_create_order
from quartermaster.application.results import CreateOrderResult
from quartermaster.domain.errors import UnknownSku
from quartermaster.domain.ids import IdempotencyKey, SkuId
from tests.integration.seed import seed_sku_locations_stock

_RunFn = Callable[
    [tuple[tuple[SkuId, int], ...], str], Coroutine[object, object, CreateOrderResult]
]


def _runner(engine: AsyncEngine) -> _RunFn:
    factory = postgres_uow_factory(engine)

    async def run(lines: tuple[tuple[SkuId, int], ...], key: str) -> CreateOrderResult:
        return await run_create_order(
            factory, lines, IdempotencyKey(key), now=system_clock, new_order_id=new_order_id
        )

    return run


async def test_missing_skus_returns_absent_subset(committed_db: AsyncEngine) -> None:
    await seed_sku_locations_stock(committed_db, "A", {})
    factory = postgres_uow_factory(committed_db)
    async with factory() as uow:
        missing = await uow.catalog.missing_skus({SkuId("A"), SkuId("B")})
    assert missing == {SkuId("B")}


async def test_create_order_persists_header_and_lines(committed_db: AsyncEngine) -> None:
    await seed_sku_locations_stock(committed_db, "A", {})
    await seed_sku_locations_stock(committed_db, "B", {})

    result = await _runner(committed_db)(((SkuId("A"), 5), (SkuId("B"), 2)), "k1")

    async with committed_db.connect() as conn:
        header = (
            await conn.execute(
                select(orders.c.state, orders.c.version).where(orders.c.order_id == result.order_id)
            )
        ).one()
        assert header.state == "created" and header.version == 1

        line_rows = (
            await conn.execute(
                select(order_line.c.sku_id, order_line.c.ordered_qty, order_line.c.allocated_qty)
                .where(order_line.c.order_id == result.order_id)
                .order_by(order_line.c.sku_id)
            )
        ).all()
    assert [(r.sku_id, r.ordered_qty, r.allocated_qty) for r in line_rows] == [
        ("A", 5, 0),
        ("B", 2, 0),
    ]


async def test_create_order_unknown_sku_is_rejected_and_cached(committed_db: AsyncEngine) -> None:
    await seed_sku_locations_stock(committed_db, "A", {})
    run = _runner(committed_db)
    with pytest.raises(UnknownSku):
        await run(((SkuId("A"), 1), (SkuId("B"), 1)), "k1")
    with pytest.raises(UnknownSku):  # replay returns the cached rejection
        await run(((SkuId("A"), 1), (SkuId("B"), 1)), "k1")


async def test_create_order_replay_is_one_order(committed_db: AsyncEngine) -> None:
    await seed_sku_locations_stock(committed_db, "A", {})
    run = _runner(committed_db)
    first = await run(((SkuId("A"), 3),), "same-key")
    second = await run(((SkuId("A"), 3),), "same-key")
    assert first == second
    async with committed_db.connect() as conn:
        count = (await conn.execute(select(orders.c.order_id))).all()
    assert len(count) == 1
