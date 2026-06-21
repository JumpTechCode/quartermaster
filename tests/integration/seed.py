# tests/integration/seed.py
"""Seed helpers and the invariant oracle for the allocate integration tests."""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncEngine

from quartermaster.adapters.postgres.identifiers import new_order_id, new_reservation_id
from quartermaster.adapters.postgres.tables import (
    location,
    order_line,
    orders,
    reservation,
    stock,
)
from quartermaster.adapters.postgres.tables import (
    sku as sku_table,
)
from quartermaster.domain.ids import LocationId, OrderId, ReservationId, SkuId
from quartermaster.domain.state_machines import OrderState, ReservationState


async def seed_sku_locations_stock(engine: AsyncEngine, sku: str, cells: dict[str, int]) -> SkuId:
    """Insert the SKU, its locations, and the given location -> on_hand stock cells."""
    async with engine.begin() as conn:
        await conn.execute(sku_table.insert().values(sku_id=sku, description="widget", unit="each"))
        for loc, on_hand in cells.items():
            await conn.execute(location.insert().values(location_id=loc, kind="shelf"))
            await conn.execute(
                stock.insert().values(
                    sku_id=sku, location_id=loc, qty_on_hand=on_hand, qty_reserved=0
                )
            )
    return SkuId(sku)


async def seed_order(engine: AsyncEngine, *, state: OrderState, lines: dict[str, int]) -> OrderId:
    """Insert an order header in ``state`` with the given sku -> ordered_qty lines."""
    order_id = new_order_id()
    async with engine.begin() as conn:
        await conn.execute(
            orders.insert().values(
                order_id=order_id, state=state.value, version=1, created_at=datetime.now(UTC)
            )
        )
        for sku, ordered in lines.items():
            await conn.execute(
                order_line.insert().values(
                    order_id=order_id,
                    sku_id=sku,
                    ordered_qty=ordered,
                    allocated_qty=0,
                    picked_qty=0,
                    shipped_qty=0,
                )
            )
    return order_id


async def seed_held_reservation(
    engine: AsyncEngine,
    *,
    sku: str,
    location: str,
    order_id: OrderId,
    qty: int,
    expires_at: datetime,
) -> ReservationId:
    """Insert a HELD reservation and raise the matching stock cell's qty_reserved.

    Mirrors the post-allocate state: a held reservation always has a matching
    reserved quantity on its stock cell, which the reaper later releases.
    """
    reservation_id = new_reservation_id()
    async with engine.begin() as conn:
        await conn.execute(
            reservation.insert().values(
                reservation_id=reservation_id,
                order_id=order_id,
                sku_id=sku,
                location_id=location,
                qty=qty,
                state=ReservationState.HELD.value,
                expires_at=expires_at,
            )
        )
        await conn.execute(
            stock.update()
            .where(stock.c.sku_id == sku, stock.c.location_id == location)
            .values(qty_reserved=stock.c.qty_reserved + qty)
        )
    return reservation_id


async def seed_sku(engine: AsyncEngine, sku: str) -> SkuId:
    """Insert a single SKU into the catalog."""
    async with engine.begin() as conn:
        await conn.execute(sku_table.insert().values(sku_id=sku, description="widget", unit="each"))
    return SkuId(sku)


async def seed_location(
    engine: AsyncEngine, location_id: str, kind: str = "receiving"
) -> LocationId:
    """Insert a single storage location."""
    async with engine.begin() as conn:
        await conn.execute(location.insert().values(location_id=location_id, kind=kind))
    return LocationId(location_id)


async def assert_invariants(engine: AsyncEngine, sku: SkuId) -> None:
    """Storage and conservation invariants for one SKU after a run (design §7)."""
    async with engine.connect() as conn:
        cells = (
            await conn.execute(
                select(stock.c.qty_on_hand, stock.c.qty_reserved).where(stock.c.sku_id == sku)
            )
        ).all()
        for cell in cells:
            assert 0 <= cell.qty_reserved <= cell.qty_on_hand  # storage invariant

        total_reserved = sum(c.qty_reserved for c in cells)
        held = (
            await conn.execute(
                select(func.coalesce(func.sum(reservation.c.qty), 0)).where(
                    reservation.c.sku_id == sku,
                    reservation.c.state == ReservationState.HELD.value,
                )
            )
        ).scalar_one()
        # conservation for this slice: reserved stock equals the sum of live held reservations
        assert total_reserved == held
