"""Repository ports (Protocols) the envelope orchestrates.

A :class:`UnitOfWork` owns exactly one transaction and exposes the repos bound
to it. ``application`` declares these contracts; ``adapters`` implement them and
are injected at the composition root. Methods are minimal — only what the commands in play need;
each repo grows as new commands arrive.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from enum import Enum, auto
from typing import Any, Protocol

from quartermaster.domain.idempotency import IdempotencyStatus
from quartermaster.domain.ids import (
    IdempotencyKey,
    LocationId,
    OrderId,
    ReceiptId,
    ReservationId,
    SkuId,
)
from quartermaster.domain.movements import Movement
from quartermaster.domain.orders import Order, OrderLine
from quartermaster.domain.receipts import Receipt, ReceiptLine
from quartermaster.domain.reservations import Reservation
from quartermaster.domain.state_machines import OrderState, ReceiptState, ReservationState


class ClaimOutcome(Enum):
    """Whether a claim INSERT won the key or found an existing row."""

    CLAIMED = auto()
    EXISTS = auto()


@dataclass(frozen=True)
class StoredResponse:
    """A persisted idempotency-key row, read back for replay."""

    command_fingerprint: str
    status: IdempotencyStatus
    response: dict[str, Any] | None


class StockRepo(Protocol):
    async def stock_locations(self, sku: SkuId) -> list[tuple[LocationId, int]]:
        """Locations holding the SKU with available > 0, ordered by location_id."""
        ...

    async def reserve_up_to(self, sku: SkuId, location: LocationId, want: int) -> int:
        """Atomically reserve min(want, available) at the cell; return the amount."""
        ...

    async def consume(self, sku: SkuId, location: LocationId, qty: int) -> bool:
        """Pick: ``on_hand -= qty, reserved -= qty`` guarded by ``reserved >= qty``.

        Returns True if the row was updated, False if the guard rejected the write.
        """
        ...

    async def release(self, sku: SkuId, location: LocationId, qty: int) -> bool:
        """Cancel/release: ``reserved -= qty`` guarded by ``reserved >= qty`` (on-hand unchanged).

        Returns True if the row was updated, False if the guard rejected the write.
        """
        ...

    async def add_on_hand(self, sku: SkuId, location: LocationId, qty: int) -> None:
        """Receive: ``qty_on_hand += qty`` at the cell, inserting it at reserved=0 if absent.

        Always succeeds — on-hand only grows on receipt; there is no availability guard.
        """
        ...


class OrderRepo(Protocol):
    async def get(self, order_id: OrderId) -> Order | None: ...
    async def get_lines(self, order_id: OrderId) -> list[OrderLine]: ...
    async def cas_state(
        self,
        order_id: OrderId,
        expected_state: OrderState,
        expected_version: int,
        new_state: OrderState,
    ) -> bool:
        """CAS the order header; bump version. False == 0 rows == conflict."""
        ...

    async def add_allocated(self, order_id: OrderId, sku_id: SkuId, qty: int) -> bool:
        """Increment allocated_qty by qty only if the result would not exceed ordered_qty.

        Returns True if the row was updated, False if the guard rejected the write
        (allocated_qty + qty > ordered_qty).  A False return is an OCC conflict signal.
        """
        ...

    async def add_picked(self, order_id: OrderId, sku_id: SkuId, qty: int) -> bool:
        """Increment picked_qty by qty only if picked_qty + qty <= allocated_qty.

        Returns True if the row was updated, False if the guard rejected the write
        (an OCC conflict signal).
        """
        ...

    async def add_shipped(self, order_id: OrderId, sku_id: SkuId, qty: int) -> bool:
        """Increment shipped_qty by qty only if shipped_qty + qty <= picked_qty.

        Returns True if the row was updated, False if the guard rejected the write
        (an OCC conflict signal).
        """
        ...

    async def insert_order(self, order: Order, lines: Sequence[OrderLine]) -> None:
        """Insert a new order header and its lines (creation; no guard)."""
        ...


class ReceiptRepo(Protocol):
    async def get(self, receipt_id: ReceiptId) -> Receipt | None: ...
    async def get_lines(self, receipt_id: ReceiptId) -> list[ReceiptLine]: ...
    async def insert_receipt(self, receipt: Receipt, lines: Sequence[ReceiptLine]) -> None:
        """Insert a new receipt header and its lines (creation; no guard)."""
        ...

    async def cas_state(
        self,
        receipt_id: ReceiptId,
        expected_state: ReceiptState,
        expected_version: int,
        new_state: ReceiptState,
    ) -> bool:
        """CAS the receipt header; bump version. False == 0 rows == conflict."""
        ...

    async def add_received(self, receipt_id: ReceiptId, sku_id: SkuId, qty: int) -> bool:
        """Increment received_qty by qty only if received_qty + qty <= expected_qty.

        Returns True if the row was updated, False if the guard rejected the write
        (an OCC/invariant signal).
        """
        ...


class ReservationRepo(Protocol):
    async def add(self, reservation: Reservation) -> None: ...

    async def held_for_order(self, order_id: OrderId) -> list[Reservation]:
        """All ``held`` reservations for the order, ordered by (sku_id, location_id)."""
        ...

    async def transition(
        self, reservation_id: ReservationId, expected: ReservationState, new: ReservationState
    ) -> bool:
        """CAS the reservation state; False == 0 rows == already finalized by another actor."""
        ...


class MovementRepo(Protocol):
    async def append(self, movement: Movement) -> None: ...


class CatalogRepo(Protocol):
    async def missing_skus(self, skus: set[SkuId]) -> set[SkuId]:
        """Return the subset of ``skus`` that do not exist in the catalog."""
        ...

    async def location_exists(self, location: LocationId) -> bool:
        """Whether ``location`` exists in the catalog."""
        ...


class IdempotencyRepo(Protocol):
    async def claim(self, key: IdempotencyKey, fingerprint: str) -> ClaimOutcome: ...
    async def load(self, key: IdempotencyKey) -> StoredResponse | None: ...
    async def finalize(
        self, key: IdempotencyKey, status: IdempotencyStatus, response: dict[str, Any] | None
    ) -> None: ...


class UnitOfWork(Protocol):
    """One transaction's worth of repositories; an async context manager."""

    stock: StockRepo
    orders: OrderRepo
    receipts: ReceiptRepo
    reservations: ReservationRepo
    movements: MovementRepo
    idempotency: IdempotencyRepo
    catalog: CatalogRepo

    async def __aenter__(self) -> UnitOfWork: ...
    async def __aexit__(self, *exc: object) -> None: ...
    async def commit(self) -> None: ...
    async def rollback(self) -> None: ...


UnitOfWorkFactory = Callable[[], UnitOfWork]
