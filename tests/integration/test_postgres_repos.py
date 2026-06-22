# tests/integration/test_postgres_repos.py
"""The Postgres repositories implement the ports against a real database."""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine

from quartermaster.adapters.postgres.identifiers import (
    new_order_id,
    new_receipt_id,
    new_reservation_id,
)
from quartermaster.adapters.postgres.tables import (
    location,
    order_line,
    orders,
    receipt_line,
    reservation,
    sku,
    stock,
)
from quartermaster.adapters.postgres.unit_of_work import PostgresUnitOfWork, postgres_uow_factory
from quartermaster.application.ports import ClaimOutcome
from quartermaster.domain.catalog import LocationKind
from quartermaster.domain.idempotency import IdempotencyStatus
from quartermaster.domain.ids import IdempotencyKey, LocationId, ReceiptId, SkuId
from quartermaster.domain.orders import Order, OrderLine
from quartermaster.domain.receipts import Receipt, ReceiptKind, ReceiptLine
from quartermaster.domain.state_machines import OrderState, ReceiptState, ReservationState
from tests.integration.seed import seed_location, seed_sku


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


async def test_consume_decrements_on_hand_and_reserved_guarded(committed_db: AsyncEngine) -> None:
    sku_id = await _seed_two_cells(committed_db, on_hand=5)
    async with PostgresUnitOfWork(committed_db) as uow:
        await uow.stock.reserve_up_to(sku_id, LocationId("L1"), 3)  # reserved 3 of 5
        assert await uow.stock.consume(sku_id, LocationId("L1"), 3) is True
        assert await uow.stock.consume(sku_id, LocationId("L1"), 1) is False  # reserved now 0
        await uow.commit()
    async with committed_db.connect() as conn:
        row = (
            await conn.execute(
                select(stock.c.qty_on_hand, stock.c.qty_reserved).where(stock.c.location_id == "L1")
            )
        ).one()
    assert (row.qty_on_hand, row.qty_reserved) == (2, 0)  # 5-3 on hand, reservation consumed


async def test_add_picked_is_guarded_by_allocated(committed_db: AsyncEngine) -> None:
    order_id = new_order_id()
    async with committed_db.begin() as conn:
        await conn.execute(sku.insert().values(sku_id="S", description="w", unit="each"))
        await conn.execute(
            orders.insert().values(
                order_id=order_id, state="allocated", version=2, created_at=datetime.now(UTC)
            )
        )
        await conn.execute(
            order_line.insert().values(
                order_id=order_id,
                sku_id="S",
                ordered_qty=5,
                allocated_qty=5,
                picked_qty=0,
                shipped_qty=0,
            )
        )
    async with PostgresUnitOfWork(committed_db) as uow:
        assert await uow.orders.add_picked(order_id, SkuId("S"), 5) is True
        assert (
            await uow.orders.add_picked(order_id, SkuId("S"), 1) is False
        )  # would exceed allocated
        await uow.commit()
    async with committed_db.connect() as conn:
        picked = (
            await conn.execute(
                select(order_line.c.picked_qty).where(order_line.c.order_id == order_id)
            )
        ).scalar_one()
    assert picked == 5


async def test_held_for_order_filters_and_orders(committed_db: AsyncEngine) -> None:
    order_id = new_order_id()
    async with committed_db.begin() as conn:
        await conn.execute(sku.insert().values(sku_id="S", description="w", unit="each"))
        await conn.execute(location.insert().values(location_id="L1", kind="shelf"))
        await conn.execute(location.insert().values(location_id="L2", kind="shelf"))
        await conn.execute(
            stock.insert().values(sku_id="S", location_id="L1", qty_on_hand=3, qty_reserved=1)
        )
        await conn.execute(
            stock.insert().values(sku_id="S", location_id="L2", qty_on_hand=3, qty_reserved=2)
        )
        await conn.execute(
            orders.insert().values(
                order_id=order_id, state="allocated", version=2, created_at=datetime.now(UTC)
            )
        )
        # two held reservations at different locations + one released (must be filtered out)
        await conn.execute(
            reservation.insert().values(
                reservation_id=new_reservation_id(),
                order_id=order_id,
                sku_id="S",
                location_id="L2",
                qty=2,
                state="held",
                expires_at=datetime.now(UTC),
            )
        )
        await conn.execute(
            reservation.insert().values(
                reservation_id=new_reservation_id(),
                order_id=order_id,
                sku_id="S",
                location_id="L1",
                qty=1,
                state="held",
                expires_at=datetime.now(UTC),
            )
        )
        await conn.execute(
            reservation.insert().values(
                reservation_id=new_reservation_id(),
                order_id=order_id,
                sku_id="S",
                location_id="L1",
                qty=1,
                state="released",
                expires_at=datetime.now(UTC),
            )
        )
    async with PostgresUnitOfWork(committed_db) as uow:
        rows = await uow.reservations.held_for_order(order_id)
        await uow.commit()
    # held only, ordered by (sku_id, location_id): L1 before L2; released excluded
    assert [(r.location_id, r.qty, r.state.value) for r in rows] == [
        (LocationId("L1"), 1, "held"),
        (LocationId("L2"), 2, "held"),
    ]


async def test_add_on_hand_inserts_then_increments(committed_db: AsyncEngine) -> None:
    await seed_sku(committed_db, "S")
    await seed_location(committed_db, "RCV", "receiving")
    factory = postgres_uow_factory(committed_db)
    async with factory() as uow:
        await uow.stock.add_on_hand(SkuId("S"), LocationId("RCV"), 3)
        await uow.stock.add_on_hand(SkuId("S"), LocationId("RCV"), 2)
        await uow.commit()
    async with committed_db.connect() as conn:
        cell = (
            await conn.execute(
                select(stock.c.qty_on_hand, stock.c.qty_reserved).where(stock.c.sku_id == "S")
            )
        ).one()
    assert (cell.qty_on_hand, cell.qty_reserved) == (5, 0)


async def test_location_kind(committed_db: AsyncEngine) -> None:
    await seed_location(committed_db, "RCV", "receiving")
    await seed_location(committed_db, "A1", "shelf")
    factory = postgres_uow_factory(committed_db)
    async with factory() as uow:
        assert await uow.catalog.location_kind(LocationId("RCV")) is LocationKind.RECEIVING
        assert await uow.catalog.location_kind(LocationId("A1")) is LocationKind.SHELF
        assert await uow.catalog.location_kind(LocationId("NOPE")) is None
        await uow.commit()


async def test_release_decrements_only_reserved_guarded(committed_db: AsyncEngine) -> None:
    sku_id = await _seed_two_cells(committed_db, on_hand=5)
    async with PostgresUnitOfWork(committed_db) as uow:
        await uow.stock.reserve_up_to(sku_id, LocationId("L1"), 3)  # reserved 3 of 5
        assert await uow.stock.release(sku_id, LocationId("L1"), 3) is True
        assert await uow.stock.release(sku_id, LocationId("L1"), 1) is False  # reserved now 0
        await uow.commit()
    async with committed_db.connect() as conn:
        row = (
            await conn.execute(
                select(stock.c.qty_on_hand, stock.c.qty_reserved).where(stock.c.location_id == "L1")
            )
        ).one()
    assert (row.qty_on_hand, row.qty_reserved) == (5, 0)  # on_hand untouched, reservation released


async def test_add_shipped_is_guarded_by_picked(committed_db: AsyncEngine) -> None:
    order_id = new_order_id()
    async with committed_db.begin() as conn:
        await conn.execute(sku.insert().values(sku_id="S", description="w", unit="each"))
        await conn.execute(
            orders.insert().values(
                order_id=order_id, state="packed", version=4, created_at=datetime.now(UTC)
            )
        )
        await conn.execute(
            order_line.insert().values(
                order_id=order_id,
                sku_id="S",
                ordered_qty=5,
                allocated_qty=5,
                picked_qty=5,
                shipped_qty=0,
            )
        )
    async with PostgresUnitOfWork(committed_db) as uow:
        assert await uow.orders.add_shipped(order_id, SkuId("S"), 5) is True
        assert await uow.orders.add_shipped(order_id, SkuId("S"), 1) is False  # would exceed picked
        await uow.commit()
    async with committed_db.connect() as conn:
        shipped = (
            await conn.execute(
                select(order_line.c.shipped_qty).where(order_line.c.order_id == order_id)
            )
        ).scalar_one()
    assert shipped == 5


async def test_transition_is_a_state_cas(committed_db: AsyncEngine) -> None:
    order_id = new_order_id()
    rid = new_reservation_id()
    async with committed_db.begin() as conn:
        await conn.execute(sku.insert().values(sku_id="S", description="w", unit="each"))
        await conn.execute(location.insert().values(location_id="L1", kind="shelf"))
        await conn.execute(
            orders.insert().values(
                order_id=order_id, state="allocated", version=2, created_at=datetime.now(UTC)
            )
        )
        await conn.execute(
            stock.insert().values(sku_id="S", location_id="L1", qty_on_hand=1, qty_reserved=1)
        )
        await conn.execute(
            reservation.insert().values(
                reservation_id=rid,
                order_id=order_id,
                sku_id="S",
                location_id="L1",
                qty=1,
                state="held",
                expires_at=datetime.now(UTC),
            )
        )
    async with PostgresUnitOfWork(committed_db) as uow:
        assert (
            await uow.reservations.transition(rid, ReservationState.HELD, ReservationState.CONSUMED)
            is True
        )
        # second attempt loses: state is no longer 'held'
        assert (
            await uow.reservations.transition(rid, ReservationState.HELD, ReservationState.CONSUMED)
            is False
        )
        await uow.commit()


async def test_insert_order_persists_all_lines(committed_db: AsyncEngine) -> None:
    """insert_order batches the line inserts; verify every line lands with its own values.

    Uses two distinct SKUs so the batch must land both rows, each carrying its own
    quantity -- the multi-row case the batched insert exists for.
    """
    await _seed_two_cells(committed_db, on_hand=10)
    async with committed_db.begin() as conn:
        await conn.execute(sku.insert().values(sku_id="T", description="w", unit="each"))
    order_id = new_order_id()
    order = Order(
        order_id=order_id,
        state=OrderState.CREATED,
        version=1,
        created_at=datetime.now(UTC),
    )
    lines = [
        OrderLine(
            order_id=order_id, sku_id=SkuId("S"), ordered=3, allocated=0, picked=0, shipped=0
        ),
        OrderLine(
            order_id=order_id, sku_id=SkuId("T"), ordered=7, allocated=0, picked=0, shipped=0
        ),
    ]
    async with PostgresUnitOfWork(committed_db) as uow:
        await uow.orders.insert_order(order, lines)
        await uow.commit()
    async with committed_db.connect() as conn:
        order_rows = (
            await conn.execute(select(orders.c.order_id).where(orders.c.order_id == order_id))
        ).all()
        line_rows = (
            await conn.execute(
                select(order_line.c.sku_id, order_line.c.ordered_qty).where(
                    order_line.c.order_id == order_id
                )
            )
        ).all()
    assert len(order_rows) == 1
    # every line in the batch landed, each with its own quantity (no drops, no cross-talk)
    assert {row.sku_id: row.ordered_qty for row in line_rows} == {"S": 3, "T": 7}


async def test_remove_on_hand_guarded_by_available(committed_db: AsyncEngine) -> None:
    await _seed_two_cells(committed_db, on_hand=5)  # S at shelf L1/L2, on_hand 5 each
    factory = postgres_uow_factory(committed_db)
    async with factory() as uow:
        await uow.stock.reserve_up_to(SkuId("S"), LocationId("L1"), 2)  # reserved 2 of 5
        assert await uow.stock.remove_on_hand(SkuId("S"), LocationId("L1"), 3) is True  # avail 3
        assert (
            await uow.stock.remove_on_hand(SkuId("S"), LocationId("L1"), 1) is False
        )  # 2 left, reserved
        await uow.commit()
    async with committed_db.connect() as conn:
        cell = (
            await conn.execute(
                select(stock.c.qty_on_hand, stock.c.qty_reserved).where(stock.c.location_id == "L1")
            )
        ).one()
    assert (cell.qty_on_hand, cell.qty_reserved) == (2, 2)  # 5-3 on hand; reserved untouched


async def test_stock_locations_excludes_non_shelf(committed_db: AsyncEngine) -> None:
    await seed_sku(committed_db, "S")
    await seed_location(committed_db, "RCV", "receiving")
    await seed_location(committed_db, "A1", "shelf")
    async with committed_db.begin() as conn:
        await conn.execute(
            stock.insert().values(sku_id="S", location_id="RCV", qty_on_hand=10, qty_reserved=0)
        )
        await conn.execute(
            stock.insert().values(sku_id="S", location_id="A1", qty_on_hand=4, qty_reserved=0)
        )
    factory = postgres_uow_factory(committed_db)
    async with factory() as uow:
        locs = await uow.stock.stock_locations(SkuId("S"))
        await uow.commit()
    assert locs == [(LocationId("A1"), 4)]  # receiving cell excluded; only the shelf


async def test_insert_order_with_no_lines(committed_db: AsyncEngine) -> None:
    """insert_order with an empty lines sequence still inserts the order row only."""
    order_id = new_order_id()
    order = Order(
        order_id=order_id,
        state=OrderState.CREATED,
        version=1,
        created_at=datetime.now(UTC),
    )
    async with PostgresUnitOfWork(committed_db) as uow:
        await uow.orders.insert_order(order, [])
        await uow.commit()
    async with committed_db.connect() as conn:
        order_query = select(orders.c.order_id).where(orders.c.order_id == order_id)
        order_rows = (await conn.execute(order_query)).all()
        line_query = select(order_line.c.order_id).where(order_line.c.order_id == order_id)
        line_rows = (await conn.execute(line_query)).all()
    assert len(order_rows) == 1
    assert len(line_rows) == 0


def _supplier_receipt(receipt_id: ReceiptId) -> Receipt:
    return Receipt(
        receipt_id=receipt_id,
        kind=ReceiptKind.SUPPLIER_RECEIPT,
        state=ReceiptState.ARRIVED,
        version=1,
        created_at=datetime.now(UTC),
        origin_order_id=None,
    )


async def test_insert_receipt_persists_all_lines(committed_db: AsyncEngine) -> None:
    """insert_receipt batches the line inserts; verify every line lands with its own values.

    Mirrors test_insert_order_persists_all_lines: two distinct SKUs so the batch
    must land both rows, each carrying its own expected/received quantity.
    """
    await seed_sku(committed_db, "S")
    await seed_sku(committed_db, "T")
    receipt_id = new_receipt_id()
    lines = [
        ReceiptLine(receipt_id=receipt_id, sku_id=SkuId("S"), expected=3, received=0),
        ReceiptLine(receipt_id=receipt_id, sku_id=SkuId("T"), expected=7, received=2),
    ]
    async with PostgresUnitOfWork(committed_db) as uow:
        await uow.receipts.insert_receipt(_supplier_receipt(receipt_id), lines)
        await uow.commit()
    async with committed_db.connect() as conn:
        line_rows = (
            await conn.execute(
                select(
                    receipt_line.c.sku_id,
                    receipt_line.c.expected_qty,
                    receipt_line.c.received_qty,
                ).where(receipt_line.c.receipt_id == receipt_id)
            )
        ).all()
    # every line in the batch landed, each with its own quantities (no drops, no cross-talk)
    assert {r.sku_id: (r.expected_qty, r.received_qty) for r in line_rows} == {
        "S": (3, 0),
        "T": (7, 2),
    }


async def test_insert_receipt_with_no_lines(committed_db: AsyncEngine) -> None:
    """insert_receipt with an empty lines sequence still inserts the receipt row only."""
    receipt_id = new_receipt_id()
    async with PostgresUnitOfWork(committed_db) as uow:
        await uow.receipts.insert_receipt(_supplier_receipt(receipt_id), [])
        await uow.commit()
    async with committed_db.connect() as conn:
        line_rows = (
            await conn.execute(
                select(receipt_line.c.receipt_id).where(receipt_line.c.receipt_id == receipt_id)
            )
        ).all()
    assert len(line_rows) == 0
