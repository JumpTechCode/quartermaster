"""The Postgres UnitOfWork and repositories — explicit Core SQL for the command
path. The conditional ``WHERE`` / ``FOR UPDATE`` re-read is the concurrency
guard under READ COMMITTED (design spec §5, §8); the ORM is deliberately unused.

READ COMMITTED is not assumed from the cluster default: the engine pins it on
every connection (``create_engine``, issue #71), so the guards below are reasoned
against the level the code enforces, not the one the operator happens to ship.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from typing import Any

from sqlalchemy import func, select, text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from quartermaster.adapters.postgres.tables import (
    idempotency_key,
    movement,
    order_line,
    orders,
    receipt,
    receipt_line,
    reservation,
    sku,
    stock,
)
from quartermaster.adapters.postgres.tables import (
    location as location_table,
)
from quartermaster.application.ports import (
    CatalogRepo,
    ClaimOutcome,
    IdempotencyRepo,
    LineQuantities,
    MovementRepo,
    MovementTotal,
    OrderRepo,
    ReceiptRepo,
    ReservationRepo,
    StockCell,
    StockRepo,
    StoredResponse,
    UnitOfWork,
    UnitOfWorkFactory,
)
from quartermaster.domain.catalog import LocationKind
from quartermaster.domain.idempotency import IdempotencyStatus
from quartermaster.domain.ids import (
    IdempotencyKey,
    LocationId,
    OrderId,
    ReceiptId,
    ReservationId,
    SkuId,
)
from quartermaster.domain.movements import Movement, MovementType
from quartermaster.domain.orders import Order, OrderLine
from quartermaster.domain.receipts import Receipt, ReceiptKind, ReceiptLine
from quartermaster.domain.reservations import Reservation
from quartermaster.domain.state_machines import OrderState, ReceiptState, ReservationState

_RESERVE_UP_TO = text(
    """
    WITH picked AS (
        SELECT sku_id, location_id, LEAST(:want, qty_on_hand - qty_reserved) AS take
          FROM stock
         WHERE sku_id = :sku AND location_id = :loc AND qty_on_hand - qty_reserved > 0
         FOR UPDATE
    )
    UPDATE stock s
       SET qty_reserved = s.qty_reserved + p.take
      FROM picked p
     WHERE s.sku_id = p.sku_id AND s.location_id = p.location_id
    RETURNING p.take
    """
)


class PgStockRepo:
    def __init__(self, conn: AsyncConnection) -> None:
        self._conn = conn

    async def stock_locations(self, sku: SkuId) -> list[tuple[LocationId, int]]:
        available = stock.c.qty_on_hand - stock.c.qty_reserved
        rows = await self._conn.execute(
            select(stock.c.location_id, available.label("available"))
            .join(location_table, location_table.c.location_id == stock.c.location_id)
            .where(
                stock.c.sku_id == sku,
                available > 0,
                location_table.c.kind == LocationKind.SHELF.value,
            )
            .order_by(stock.c.location_id)
        )
        return [(LocationId(r.location_id), int(r.available)) for r in rows]

    async def reserve_up_to(self, sku: SkuId, location: LocationId, want: int) -> int:
        row = (
            await self._conn.execute(_RESERVE_UP_TO, {"want": want, "sku": sku, "loc": location})
        ).first()
        return int(row.take) if row is not None else 0

    async def consume(self, sku: SkuId, location: LocationId, qty: int) -> bool:
        result = await self._conn.execute(
            stock.update()
            .where(
                stock.c.sku_id == sku,
                stock.c.location_id == location,
                stock.c.qty_reserved >= qty,
            )
            .values(
                qty_on_hand=stock.c.qty_on_hand - qty,
                qty_reserved=stock.c.qty_reserved - qty,
            )
        )
        return result.rowcount == 1

    async def release(self, sku: SkuId, location: LocationId, qty: int) -> bool:
        result = await self._conn.execute(
            stock.update()
            .where(
                stock.c.sku_id == sku,
                stock.c.location_id == location,
                stock.c.qty_reserved >= qty,
            )
            .values(qty_reserved=stock.c.qty_reserved - qty)
        )
        return result.rowcount == 1

    async def add_on_hand(self, sku: SkuId, location: LocationId, qty: int) -> None:
        stmt = pg_insert(stock).values(
            sku_id=sku, location_id=location, qty_on_hand=qty, qty_reserved=0
        )
        stmt = stmt.on_conflict_do_update(
            index_elements=[stock.c.sku_id, stock.c.location_id],
            # Increment the EXISTING committed row value, not excluded.qty_on_hand
            # (the proposed insert). Under READ COMMITTED the ON CONFLICT path takes
            # the row lock, so a concurrent receiver to the same cell blocks, re-reads
            # the committed total, and adds — no lost update. Do not "simplify" to excluded.
            set_={"qty_on_hand": stock.c.qty_on_hand + qty},
        )
        await self._conn.execute(stmt)

    async def remove_on_hand(self, sku: SkuId, location: LocationId, qty: int) -> bool:
        result = await self._conn.execute(
            stock.update()
            .where(
                stock.c.sku_id == sku,
                stock.c.location_id == location,
                stock.c.qty_on_hand - stock.c.qty_reserved >= qty,
            )
            .values(qty_on_hand=stock.c.qty_on_hand - qty)
        )
        return result.rowcount == 1

    async def all_cells(self) -> list[StockCell]:
        rows = await self._conn.execute(
            select(
                stock.c.sku_id,
                stock.c.location_id,
                stock.c.qty_on_hand,
                stock.c.qty_reserved,
            )
        )
        return [
            StockCell(
                sku_id=SkuId(r.sku_id),
                location_id=LocationId(r.location_id),
                on_hand=int(r.qty_on_hand),
                reserved=int(r.qty_reserved),
            )
            for r in rows
        ]


class PgOrderRepo:
    def __init__(self, conn: AsyncConnection) -> None:
        self._conn = conn

    async def get(self, order_id: OrderId) -> Order | None:
        row = (
            await self._conn.execute(select(orders).where(orders.c.order_id == order_id))
        ).first()
        if row is None:
            return None
        return Order(
            order_id=OrderId(row.order_id),
            state=OrderState(row.state),
            version=int(row.version),
            created_at=row.created_at,
        )

    async def get_lines(self, order_id: OrderId) -> list[OrderLine]:
        rows = await self._conn.execute(
            select(order_line)
            .where(order_line.c.order_id == order_id)
            .order_by(order_line.c.sku_id)
        )
        return [
            OrderLine(
                order_id=OrderId(r.order_id),
                sku_id=SkuId(r.sku_id),
                ordered=int(r.ordered_qty),
                allocated=int(r.allocated_qty),
                picked=int(r.picked_qty),
                shipped=int(r.shipped_qty),
            )
            for r in rows
        ]

    async def cas_state(
        self,
        order_id: OrderId,
        expected_state: OrderState,
        expected_version: int,
        new_state: OrderState,
    ) -> bool:
        result = await self._conn.execute(
            orders.update()
            .where(
                orders.c.order_id == order_id,
                orders.c.state == expected_state.value,
                orders.c.version == expected_version,
            )
            .values(state=new_state.value, version=orders.c.version + 1)
        )
        return result.rowcount == 1

    async def add_allocated(self, order_id: OrderId, sku_id: SkuId, qty: int) -> bool:
        result = await self._conn.execute(
            order_line.update()
            .where(
                order_line.c.order_id == order_id,
                order_line.c.sku_id == sku_id,
                order_line.c.allocated_qty + qty <= order_line.c.ordered_qty,
            )
            .values(allocated_qty=order_line.c.allocated_qty + qty)
        )
        return result.rowcount == 1

    async def remove_allocated(self, order_id: OrderId, sku_id: SkuId, qty: int) -> bool:
        result = await self._conn.execute(
            order_line.update()
            .where(
                order_line.c.order_id == order_id,
                order_line.c.sku_id == sku_id,
                order_line.c.allocated_qty - qty >= order_line.c.picked_qty,
            )
            .values(allocated_qty=order_line.c.allocated_qty - qty)
        )
        return result.rowcount == 1

    async def mark_backordered(self, order_id: OrderId) -> bool:
        result = await self._conn.execute(
            orders.update()
            .where(
                orders.c.order_id == order_id,
                orders.c.state == OrderState.ALLOCATED.value,
            )
            .values(state=OrderState.BACKORDERED.value, version=orders.c.version + 1)
        )
        return result.rowcount == 1

    async def add_picked(self, order_id: OrderId, sku_id: SkuId, qty: int) -> bool:
        result = await self._conn.execute(
            order_line.update()
            .where(
                order_line.c.order_id == order_id,
                order_line.c.sku_id == sku_id,
                order_line.c.picked_qty + qty <= order_line.c.allocated_qty,
            )
            .values(picked_qty=order_line.c.picked_qty + qty)
        )
        return result.rowcount == 1

    async def add_shipped(self, order_id: OrderId, sku_id: SkuId, qty: int) -> bool:
        result = await self._conn.execute(
            order_line.update()
            .where(
                order_line.c.order_id == order_id,
                order_line.c.sku_id == sku_id,
                order_line.c.shipped_qty + qty <= order_line.c.picked_qty,
            )
            .values(shipped_qty=order_line.c.shipped_qty + qty)
        )
        return result.rowcount == 1

    async def insert_order(self, order: Order, lines: Sequence[OrderLine]) -> None:
        await self._conn.execute(
            orders.insert().values(
                order_id=order.order_id,
                state=order.state.value,
                version=order.version,
                created_at=order.created_at,
            )
        )
        if lines:
            # A single multi-row INSERT (executemany form) instead of one
            # round-trip per line. Pass a list of dicts as params alongside
            # the bare insert() construct -- table.insert().values(list)
            # does not use executemany with asyncpg, this form does.
            await self._conn.execute(
                order_line.insert(),
                [
                    {
                        "order_id": line.order_id,
                        "sku_id": line.sku_id,
                        "ordered_qty": line.ordered,
                        "allocated_qty": line.allocated,
                        "picked_qty": line.picked,
                        "shipped_qty": line.shipped,
                    }
                    for line in lines
                ],
            )

    async def backordered_orders(self, limit: int) -> list[OrderId]:
        rows = await self._conn.execute(
            select(orders.c.order_id)
            .where(orders.c.state == OrderState.BACKORDERED.value)
            .order_by(orders.c.created_at)
            .limit(limit)
        )
        return [OrderId(r.order_id) for r in rows]

    async def shipped_by_sku(self) -> dict[SkuId, int]:
        total = func.sum(order_line.c.shipped_qty).label("total_shipped")
        rows = await self._conn.execute(
            select(order_line.c.sku_id, total).group_by(order_line.c.sku_id)
        )
        return {SkuId(r.sku_id): int(r.total_shipped) for r in rows}

    async def lines_breaking_monotonic(self) -> list[LineQuantities]:
        c = order_line.c
        monotonic = (
            (c.shipped_qty >= 0)
            & (c.shipped_qty <= c.picked_qty)
            & (c.picked_qty <= c.allocated_qty)
            & (c.allocated_qty <= c.ordered_qty)
        )
        rows = await self._conn.execute(select(order_line).where(~monotonic))
        return [
            LineQuantities(
                order_id=OrderId(r.order_id),
                sku_id=SkuId(r.sku_id),
                ordered=int(r.ordered_qty),
                allocated=int(r.allocated_qty),
                picked=int(r.picked_qty),
                shipped=int(r.shipped_qty),
            )
            for r in rows
        ]


class PgReceiptRepo:
    def __init__(self, conn: AsyncConnection) -> None:
        self._conn = conn

    async def get(self, receipt_id: ReceiptId) -> Receipt | None:
        row = (
            await self._conn.execute(select(receipt).where(receipt.c.receipt_id == receipt_id))
        ).first()
        if row is None:
            return None
        return Receipt(
            receipt_id=ReceiptId(row.receipt_id),
            kind=ReceiptKind(row.kind),
            state=ReceiptState(row.state),
            version=int(row.version),
            created_at=row.created_at,
            origin_order_id=(
                OrderId(row.origin_order_id) if row.origin_order_id is not None else None
            ),
        )

    async def get_lines(self, receipt_id: ReceiptId) -> list[ReceiptLine]:
        rows = await self._conn.execute(
            select(receipt_line)
            .where(receipt_line.c.receipt_id == receipt_id)
            .order_by(receipt_line.c.sku_id)
        )
        return [
            ReceiptLine(
                receipt_id=ReceiptId(r.receipt_id),
                sku_id=SkuId(r.sku_id),
                expected=int(r.expected_qty),
                received=int(r.received_qty),
            )
            for r in rows
        ]

    async def insert_receipt(self, receipt_obj: Receipt, lines: Sequence[ReceiptLine]) -> None:
        await self._conn.execute(
            receipt.insert().values(
                receipt_id=receipt_obj.receipt_id,
                kind=receipt_obj.kind.value,
                state=receipt_obj.state.value,
                version=receipt_obj.version,
                created_at=receipt_obj.created_at,
                origin_order_id=receipt_obj.origin_order_id,
            )
        )
        if lines:
            # A single multi-row INSERT (executemany form) instead of one
            # round-trip per line, mirroring insert_order. Pass a list of dicts
            # as params alongside the bare insert() construct -- table.insert()
            # .values(list) does not use executemany with asyncpg, this form does.
            await self._conn.execute(
                receipt_line.insert(),
                [
                    {
                        "receipt_id": line.receipt_id,
                        "sku_id": line.sku_id,
                        "expected_qty": line.expected,
                        "received_qty": line.received,
                    }
                    for line in lines
                ],
            )

    async def cas_state(
        self,
        receipt_id: ReceiptId,
        expected_state: ReceiptState,
        expected_version: int,
        new_state: ReceiptState,
    ) -> bool:
        result = await self._conn.execute(
            receipt.update()
            .where(
                receipt.c.receipt_id == receipt_id,
                receipt.c.state == expected_state.value,
                receipt.c.version == expected_version,
            )
            .values(state=new_state.value, version=receipt.c.version + 1)
        )
        return result.rowcount == 1

    async def add_received(self, receipt_id: ReceiptId, sku_id: SkuId, qty: int) -> bool:
        result = await self._conn.execute(
            receipt_line.update()
            .where(
                receipt_line.c.receipt_id == receipt_id,
                receipt_line.c.sku_id == sku_id,
                receipt_line.c.received_qty + qty <= receipt_line.c.expected_qty,
            )
            .values(received_qty=receipt_line.c.received_qty + qty)
        )
        return result.rowcount == 1


class PgCatalogRepo:
    def __init__(self, conn: AsyncConnection) -> None:
        self._conn = conn

    async def missing_skus(self, skus: set[SkuId]) -> set[SkuId]:
        if not skus:
            return set()
        rows = await self._conn.execute(select(sku.c.sku_id).where(sku.c.sku_id.in_(list(skus))))
        found = {SkuId(r.sku_id) for r in rows}
        return skus - found

    async def location_kind(self, location: LocationId) -> LocationKind | None:
        row = (
            await self._conn.execute(
                select(location_table.c.kind).where(location_table.c.location_id == location)
            )
        ).first()
        return LocationKind(row.kind) if row is not None else None


class PgReservationRepo:
    def __init__(self, conn: AsyncConnection) -> None:
        self._conn = conn

    async def add(self, res: Reservation) -> None:
        await self._conn.execute(
            reservation.insert().values(
                reservation_id=res.reservation_id,
                order_id=res.order_id,
                sku_id=res.sku_id,
                location_id=res.location_id,
                qty=res.qty,
                state=res.state.value,
                expires_at=res.expires_at,
            )
        )

    async def held_for_order(self, order_id: OrderId) -> list[Reservation]:
        rows = await self._conn.execute(
            select(reservation)
            .where(
                reservation.c.order_id == order_id,
                reservation.c.state == ReservationState.HELD.value,
            )
            .order_by(reservation.c.sku_id, reservation.c.location_id)
        )
        return [
            Reservation(
                reservation_id=ReservationId(r.reservation_id),
                order_id=OrderId(r.order_id),
                sku_id=SkuId(r.sku_id),
                location_id=LocationId(r.location_id),
                qty=int(r.qty),
                state=ReservationState(r.state),
                expires_at=r.expires_at,
            )
            for r in rows
        ]

    async def transition(
        self, reservation_id: ReservationId, expected: ReservationState, new: ReservationState
    ) -> bool:
        result = await self._conn.execute(
            reservation.update()
            .where(
                reservation.c.reservation_id == reservation_id,
                reservation.c.state == expected.value,
            )
            .values(state=new.value)
        )
        return result.rowcount == 1

    async def due_for_expiry(self, now: datetime, limit: int) -> list[Reservation]:
        rows = await self._conn.execute(
            select(reservation)
            .where(
                reservation.c.state == ReservationState.HELD.value,
                reservation.c.expires_at <= now,
            )
            .order_by(reservation.c.expires_at)
            .limit(limit)
        )
        return [
            Reservation(
                reservation_id=ReservationId(r.reservation_id),
                order_id=OrderId(r.order_id),
                sku_id=SkuId(r.sku_id),
                location_id=LocationId(r.location_id),
                qty=int(r.qty),
                state=ReservationState(r.state),
                expires_at=r.expires_at,
            )
            for r in rows
        ]


class PgMovementRepo:
    def __init__(self, conn: AsyncConnection) -> None:
        self._conn = conn

    async def append(self, mv: Movement) -> None:
        await self._conn.execute(
            movement.insert().values(
                movement_id=mv.movement_id,
                ts=mv.ts,
                type=mv.type.value,
                sku_id=mv.sku_id,
                from_location=mv.from_location,
                to_location=mv.to_location,
                qty=mv.qty,
                ref=mv.ref,
                command_id=mv.command_id,
            )
        )

    async def aggregate(self) -> list[MovementTotal]:
        total = func.sum(movement.c.qty).label("total_qty")
        rows = await self._conn.execute(
            select(
                movement.c.type,
                movement.c.sku_id,
                movement.c.from_location,
                movement.c.to_location,
                total,
            ).group_by(
                movement.c.type,
                movement.c.sku_id,
                movement.c.from_location,
                movement.c.to_location,
            )
        )
        return [
            MovementTotal(
                type=MovementType(r.type),
                sku_id=SkuId(r.sku_id),
                from_location=LocationId(r.from_location) if r.from_location is not None else None,
                to_location=LocationId(r.to_location) if r.to_location is not None else None,
                total_qty=int(r.total_qty),
            )
            for r in rows
        ]


class PgIdempotencyRepo:
    def __init__(self, conn: AsyncConnection) -> None:
        self._conn = conn

    async def claim(self, key: IdempotencyKey, fingerprint: str) -> ClaimOutcome:
        # ON CONFLICT DO NOTHING blocks on a concurrent uncommitted duplicate and
        # never poisons the transaction; a returned row means we won the claim.
        result = await self._conn.execute(
            text(
                """
                INSERT INTO idempotency_key (key, command_fingerprint, status, response, created_at)
                VALUES (:key, :fp, :status, NULL, now())
                ON CONFLICT (key) DO NOTHING
                RETURNING key
                """
            ),
            {"key": key, "fp": fingerprint, "status": IdempotencyStatus.PENDING.value},
        )
        return ClaimOutcome.CLAIMED if result.first() is not None else ClaimOutcome.EXISTS

    async def load(self, key: IdempotencyKey) -> StoredResponse | None:
        row = (
            await self._conn.execute(
                select(
                    idempotency_key.c.command_fingerprint,
                    idempotency_key.c.status,
                    idempotency_key.c.response,
                ).where(idempotency_key.c.key == key)
            )
        ).first()
        if row is None:
            return None
        return StoredResponse(
            command_fingerprint=row.command_fingerprint,
            status=IdempotencyStatus(row.status),
            response=row.response,
        )

    async def finalize(
        self, key: IdempotencyKey, status: IdempotencyStatus, response: dict[str, Any] | None
    ) -> None:
        await self._conn.execute(
            idempotency_key.update()
            .where(idempotency_key.c.key == key)
            .values(status=status.value, response=response)
        )

    async def delete_expired(self, before: datetime, limit: int) -> int:
        result = await self._conn.execute(
            text(
                """
                DELETE FROM idempotency_key
                 WHERE key IN (
                     SELECT key FROM idempotency_key
                      WHERE created_at < :before
                      ORDER BY created_at
                      LIMIT :limit
                 )
                """
            ),
            {"before": before, "limit": limit},
        )
        return result.rowcount


class PostgresUnitOfWork:
    """One transaction over an AsyncConnection, exposing the Postgres repos."""

    stock: StockRepo
    orders: OrderRepo
    receipts: ReceiptRepo
    reservations: ReservationRepo
    movements: MovementRepo
    idempotency: IdempotencyRepo
    catalog: CatalogRepo

    def __init__(self, engine: AsyncEngine) -> None:
        self._engine = engine
        self._finished = False

    async def __aenter__(self) -> PostgresUnitOfWork:
        self._conn = await self._engine.connect()
        self._trans = await self._conn.begin()
        self.stock = PgStockRepo(self._conn)
        self.orders = PgOrderRepo(self._conn)
        self.receipts = PgReceiptRepo(self._conn)
        self.reservations = PgReservationRepo(self._conn)
        self.movements = PgMovementRepo(self._conn)
        self.idempotency = PgIdempotencyRepo(self._conn)
        self.catalog = PgCatalogRepo(self._conn)
        return self

    async def __aexit__(self, *exc: object) -> None:
        if not self._finished:
            await self._trans.rollback()
        await self._conn.close()

    async def commit(self) -> None:
        await self._trans.commit()
        self._finished = True

    async def rollback(self) -> None:
        await self._trans.rollback()
        self._finished = True


def postgres_uow_factory(engine: AsyncEngine) -> UnitOfWorkFactory:
    """Return a UnitOfWorkFactory bound to ``engine``."""

    def factory() -> UnitOfWork:
        return PostgresUnitOfWork(engine)

    return factory
