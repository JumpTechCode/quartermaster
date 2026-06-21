# src/quartermaster/adapters/postgres/tables.py
"""SQLAlchemy Core schema: the single source of truth for the §3 data model.

These ``Table`` definitions back the adapter's query construction and are the
authority the Alembic migration builds (a drift test keeps them in parity). The
CHECK constraints are written faithfully because at the storage layer they *are*
the inventory invariants (design spec §3): they make over-decrement and
over-reserve impossible, not merely discouraged. Enum-like columns are
``text + CHECK … IN (…)`` whose allowed values are sourced directly from the
domain ``StrEnum``s, so the database and the domain can never drift.
"""

from __future__ import annotations

from collections.abc import Iterable

from sqlalchemy import (
    CheckConstraint,
    Column,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    MetaData,
    Table,
    Text,
)
from sqlalchemy.dialects.postgresql import JSONB, TIMESTAMP, UUID

from quartermaster.domain.catalog import LocationKind
from quartermaster.domain.idempotency import IdempotencyStatus
from quartermaster.domain.movements import MovementType
from quartermaster.domain.receipts import ReceiptKind
from quartermaster.domain.state_machines import OrderState, ReceiptState, ReservationState

NAMING_CONVENTION = {
    "ix": "ix_%(table_name)s_%(column_0_N_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_N_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
}

metadata = MetaData(naming_convention=NAMING_CONVENTION)


def _enum_check(column: str, values: Iterable[str]) -> str:
    """Render a ``column IN ('a', 'b', …)`` SQL predicate from enum values."""
    rendered = ", ".join(f"'{value}'" for value in values)
    return f"{column} IN ({rendered})"


sku = Table(
    "sku",
    metadata,
    Column("sku_id", Text, primary_key=True),
    Column("description", Text, nullable=False),
    Column("unit", Text, nullable=False),
)

location = Table(
    "location",
    metadata,
    Column("location_id", Text, primary_key=True),
    Column("kind", Text, nullable=False),
    CheckConstraint(_enum_check("kind", [k.value for k in LocationKind]), name="kind"),
)

stock = Table(
    "stock",
    metadata,
    Column("sku_id", Text, ForeignKey("sku.sku_id"), primary_key=True),
    Column("location_id", Text, ForeignKey("location.location_id"), primary_key=True),
    Column("qty_on_hand", Integer, nullable=False),
    Column("qty_reserved", Integer, nullable=False),
    CheckConstraint("qty_on_hand >= 0", name="on_hand_nonneg"),
    CheckConstraint("qty_reserved >= 0", name="reserved_nonneg"),
    CheckConstraint("qty_reserved <= qty_on_hand", name="reserved_le_on_hand"),
)

orders = Table(
    "orders",
    metadata,
    Column("order_id", UUID(as_uuid=True), primary_key=True),
    Column("state", Text, nullable=False),
    Column("version", Integer, nullable=False),
    Column("created_at", TIMESTAMP(timezone=True), nullable=False),
    CheckConstraint(_enum_check("state", [s.value for s in OrderState]), name="state"),
    CheckConstraint("version >= 1", name="version_positive"),
    Index("ix_orders_state_created_at", "state", "created_at"),
)

order_line = Table(
    "order_line",
    metadata,
    Column("order_id", UUID(as_uuid=True), ForeignKey("orders.order_id"), primary_key=True),
    Column("sku_id", Text, ForeignKey("sku.sku_id"), primary_key=True),
    Column("ordered_qty", Integer, nullable=False),
    Column("allocated_qty", Integer, nullable=False),
    Column("picked_qty", Integer, nullable=False),
    Column("shipped_qty", Integer, nullable=False),
    CheckConstraint(
        "0 <= shipped_qty AND shipped_qty <= picked_qty "
        "AND picked_qty <= allocated_qty AND allocated_qty <= ordered_qty",
        name="monotonic",
    ),
)

receipt = Table(
    "receipt",
    metadata,
    Column("receipt_id", UUID(as_uuid=True), primary_key=True),
    Column("kind", Text, nullable=False),
    Column("state", Text, nullable=False),
    Column("version", Integer, nullable=False),
    Column("created_at", TIMESTAMP(timezone=True), nullable=False),
    Column("origin_order_id", UUID(as_uuid=True), ForeignKey("orders.order_id"), nullable=True),
    CheckConstraint(_enum_check("kind", [k.value for k in ReceiptKind]), name="kind"),
    CheckConstraint(_enum_check("state", [s.value for s in ReceiptState]), name="state"),
    CheckConstraint("version >= 1", name="version_positive"),
    CheckConstraint(
        "(kind = 'customer_rma') = (origin_order_id IS NOT NULL)",
        name="rma_origin",
    ),
)

receipt_line = Table(
    "receipt_line",
    metadata,
    Column("receipt_id", UUID(as_uuid=True), ForeignKey("receipt.receipt_id"), primary_key=True),
    Column("sku_id", Text, ForeignKey("sku.sku_id"), primary_key=True),
    Column("expected_qty", Integer, nullable=False),
    Column("received_qty", Integer, nullable=False),
    CheckConstraint(
        "0 <= received_qty AND received_qty <= expected_qty",
        name="received_le_expected",
    ),
)

reservation = Table(
    "reservation",
    metadata,
    Column("reservation_id", UUID(as_uuid=True), primary_key=True),
    Column("order_id", UUID(as_uuid=True), ForeignKey("orders.order_id"), nullable=False),
    Column("sku_id", Text, nullable=False),
    Column("location_id", Text, nullable=False),
    Column("qty", Integer, nullable=False),
    Column("state", Text, nullable=False),
    Column("expires_at", TIMESTAMP(timezone=True), nullable=False),
    ForeignKeyConstraint(
        ["sku_id", "location_id"],
        ["stock.sku_id", "stock.location_id"],
    ),
    CheckConstraint("qty > 0", name="qty_positive"),
    CheckConstraint(_enum_check("state", [s.value for s in ReservationState]), name="state"),
    Index("ix_reservation_state_expires_at", "state", "expires_at"),
)

movement = Table(
    "movement",
    metadata,
    Column("movement_id", UUID(as_uuid=True), primary_key=True),
    Column("ts", TIMESTAMP(timezone=True), nullable=False),
    Column("type", Text, nullable=False),
    Column("sku_id", Text, ForeignKey("sku.sku_id"), nullable=False),
    Column("from_location", Text, ForeignKey("location.location_id"), nullable=True),
    Column("to_location", Text, ForeignKey("location.location_id"), nullable=True),
    Column("qty", Integer, nullable=False),
    Column("ref", UUID(as_uuid=True), nullable=False),  # polymorphic (order|receipt): no FK
    Column("command_id", Text, nullable=False),  # idempotency key: no FK (TTL-reaped, §5.5)
    CheckConstraint("qty > 0", name="qty_positive"),
    CheckConstraint(_enum_check("type", [t.value for t in MovementType]), name="type"),
    Index("ix_movement_sku_id_ts", "sku_id", "ts"),
)

idempotency_key = Table(
    "idempotency_key",
    metadata,
    Column("key", Text, primary_key=True),
    Column("command_fingerprint", Text, nullable=False),
    Column("status", Text, nullable=False),
    Column("response", JSONB, nullable=True),
    Column("created_at", TIMESTAMP(timezone=True), nullable=False),
    CheckConstraint(_enum_check("status", [s.value for s in IdempotencyStatus]), name="status"),
)
