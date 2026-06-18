"""Repository ports (Protocols) the envelope orchestrates.

A :class:`UnitOfWork` owns exactly one transaction and exposes the repos bound
to it. ``application`` declares these contracts; ``adapters`` implement them and
are injected at the composition root. Methods are minimal — only what the
``allocate`` command needs; each repo grows with future commands.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum, auto
from typing import Any, Protocol

from quartermaster.domain.idempotency import IdempotencyStatus
from quartermaster.domain.ids import (
    IdempotencyKey,
    LocationId,
    OrderId,
    SkuId,
)
from quartermaster.domain.movements import Movement
from quartermaster.domain.orders import Order, OrderLine
from quartermaster.domain.reservations import Reservation
from quartermaster.domain.state_machines import OrderState


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

    async def add_allocated(self, order_id: OrderId, sku: SkuId, qty: int) -> None: ...


class ReservationRepo(Protocol):
    async def add(self, reservation: Reservation) -> None: ...


class MovementRepo(Protocol):
    async def append(self, movement: Movement) -> None: ...


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
    reservations: ReservationRepo
    movements: MovementRepo
    idempotency: IdempotencyRepo

    async def __aenter__(self) -> UnitOfWork: ...
    async def __aexit__(self, *exc: object) -> None: ...
    async def commit(self) -> None: ...
    async def rollback(self) -> None: ...


UnitOfWorkFactory = Callable[[], UnitOfWork]
