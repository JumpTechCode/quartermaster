# tests/integration/test_schema_constraints.py
"""Postgres enforces the §3 storage-layer invariants (they are impossible to
violate), and a valid document round-trips through the schema."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

import pytest
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncConnection

from quartermaster.adapters.postgres.identifiers import new_uuid7
from quartermaster.adapters.postgres.tables import (
    location,
    order_line,
    orders,
    receipt,
    receipt_line,
    reservation,
    sku,
    stock,
)


def _now() -> datetime:
    return datetime.now(UTC)


async def _seed_reference(db: AsyncConnection) -> None:
    """Insert a valid sku and location (the reference data the other rows need)."""
    await db.execute(sku.insert().values(sku_id="SKU1", description="widget", unit="each"))
    await db.execute(location.insert().values(location_id="L1", kind="shelf"))


async def _seed(db: AsyncConnection) -> UUID:
    """Insert valid reference data, an order, and a stock cell; return the order id."""
    await _seed_reference(db)
    order_id = new_uuid7()
    await db.execute(
        orders.insert().values(order_id=order_id, state="created", version=1, created_at=_now())
    )
    await db.execute(
        stock.insert().values(sku_id="SKU1", location_id="L1", qty_on_hand=10, qty_reserved=0)
    )
    return order_id


async def test_stock_reserved_cannot_exceed_on_hand(db: AsyncConnection) -> None:
    # Seed only reference data — no prior stock row — so the violating insert
    # fails on the reserved<=on_hand CHECK, not on a duplicate primary key.
    await _seed_reference(db)
    with pytest.raises(IntegrityError):
        await db.execute(
            stock.insert().values(sku_id="SKU1", location_id="L1", qty_on_hand=5, qty_reserved=9)
        )


async def test_stock_quantities_cannot_be_negative(db: AsyncConnection) -> None:
    await _seed_reference(db)
    with pytest.raises(IntegrityError):
        await db.execute(
            stock.insert().values(sku_id="SKU1", location_id="L1", qty_on_hand=-1, qty_reserved=0)
        )


async def test_order_line_quantities_must_be_monotonic(db: AsyncConnection) -> None:
    order_id = await _seed(db)
    with pytest.raises(IntegrityError):
        await db.execute(
            order_line.insert().values(
                order_id=order_id,
                sku_id="SKU1",
                ordered_qty=10,
                allocated_qty=0,
                picked_qty=0,
                shipped_qty=5,  # shipped > picked: violates the chain
            )
        )


async def test_reservation_quantity_must_be_positive(db: AsyncConnection) -> None:
    order_id = await _seed(db)
    with pytest.raises(IntegrityError):
        await db.execute(
            reservation.insert().values(
                reservation_id=new_uuid7(),
                order_id=order_id,
                sku_id="SKU1",
                location_id="L1",
                qty=0,  # must be > 0
                state="held",
                expires_at=_now(),
            )
        )


async def test_customer_rma_requires_an_origin_order(db: AsyncConnection) -> None:
    with pytest.raises(IntegrityError):
        await db.execute(
            receipt.insert().values(
                receipt_id=new_uuid7(),
                kind="customer_rma",
                state="expected",
                version=1,
                created_at=_now(),
                origin_order_id=None,  # an RMA must reference an order
            )
        )


async def test_receipt_line_received_cannot_exceed_expected(db: AsyncConnection) -> None:
    await _seed_reference(db)
    receipt_id = new_uuid7()
    await db.execute(
        receipt.insert().values(
            receipt_id=receipt_id,
            kind="supplier_receipt",
            state="expected",
            version=1,
            created_at=_now(),
            origin_order_id=None,
        )
    )
    with pytest.raises(IntegrityError):
        await db.execute(
            receipt_line.insert().values(
                receipt_id=receipt_id,
                sku_id="SKU1",
                expected_qty=5,
                received_qty=9,  # received > expected: violates the line check
            )
        )


async def test_order_state_must_be_in_the_allowed_set(db: AsyncConnection) -> None:
    with pytest.raises(IntegrityError):
        await db.execute(
            orders.insert().values(
                order_id=new_uuid7(), state="bogus", version=1, created_at=_now()
            )
        )


async def test_valid_order_round_trips_with_a_uuid7_id(db: AsyncConnection) -> None:
    order_id = new_uuid7()
    await db.execute(
        orders.insert().values(order_id=order_id, state="created", version=1, created_at=_now())
    )
    result = await db.execute(orders.select().where(orders.c.order_id == order_id))
    row = result.one()
    assert row.order_id == order_id
    assert row.state == "created"
    assert row.version == 1
