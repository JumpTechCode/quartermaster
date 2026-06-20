"""Record-only fakes for unit-testing envelope orchestration and allocate logic.

These fakes record calls and return canned/in-memory results. They exist to test
*wiring* (transaction ordering, commit/rollback choice, greedy loop arithmetic),
never concurrency — races are proven against real Postgres in the integration
suite.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from quartermaster.application.ports import (
    CatalogRepo,
    ClaimOutcome,
    IdempotencyRepo,
    MovementRepo,
    OrderRepo,
    ReservationRepo,
    StockRepo,
    StoredResponse,
    UnitOfWork,
    UnitOfWorkFactory,
)
from quartermaster.domain.idempotency import IdempotencyStatus
from quartermaster.domain.ids import (
    IdempotencyKey,
    LocationId,
    OrderId,
    ReservationId,
    SkuId,
)
from quartermaster.domain.movements import Movement
from quartermaster.domain.orders import Order, OrderLine
from quartermaster.domain.reservations import Reservation
from quartermaster.domain.state_machines import OrderState, ReservationState


class FakeStockRepo:
    def __init__(self, cells: dict[tuple[SkuId, LocationId], int] | None = None) -> None:
        # cells maps (sku, location) -> available units
        self.cells = cells or {}
        self.reserve_calls: list[tuple[SkuId, LocationId, int]] = []
        self.consume_result = True
        self.consume_calls: list[tuple[SkuId, LocationId, int]] = []
        self.release_result = True
        self.release_calls: list[tuple[SkuId, LocationId, int]] = []

    async def stock_locations(self, sku: SkuId) -> list[tuple[LocationId, int]]:
        locs = [(loc, avail) for (s, loc), avail in self.cells.items() if s == sku and avail > 0]
        return sorted(locs, key=lambda pair: pair[0])

    async def reserve_up_to(self, sku: SkuId, location: LocationId, want: int) -> int:
        self.reserve_calls.append((sku, location, want))
        avail = self.cells.get((sku, location), 0)
        take = min(want, avail)
        self.cells[(sku, location)] = avail - take
        return take

    async def consume(self, sku: SkuId, location: LocationId, qty: int) -> bool:
        self.consume_calls.append((sku, location, qty))
        return self.consume_result

    async def release(self, sku: SkuId, location: LocationId, qty: int) -> bool:
        self.release_calls.append((sku, location, qty))
        return self.release_result


class FakeOrderRepo:
    def __init__(
        self,
        order: Order | None = None,
        lines: list[OrderLine] | None = None,
        cas_result: bool = True,
        *,
        add_allocated_result: bool = True,
        add_picked_result: bool = True,
        add_shipped_result: bool = True,
    ) -> None:
        self.order = order
        self.lines = lines or []
        self.cas_result = cas_result
        self.add_allocated_result = add_allocated_result
        self.add_picked_result = add_picked_result
        self.add_shipped_result = add_shipped_result
        self.cas_calls: list[tuple[OrderId, OrderState, int, OrderState]] = []
        self.allocated: list[tuple[OrderId, SkuId, int]] = []
        self.picked: list[tuple[OrderId, SkuId, int]] = []
        self.shipped: list[tuple[OrderId, SkuId, int]] = []
        self.inserted: list[tuple[Order, list[OrderLine]]] = []

    async def get(self, order_id: OrderId) -> Order | None:
        return self.order

    async def get_lines(self, order_id: OrderId) -> list[OrderLine]:
        return list(self.lines)

    async def cas_state(
        self,
        order_id: OrderId,
        expected_state: OrderState,
        expected_version: int,
        new_state: OrderState,
    ) -> bool:
        self.cas_calls.append((order_id, expected_state, expected_version, new_state))
        return self.cas_result

    async def add_allocated(self, order_id: OrderId, sku_id: SkuId, qty: int) -> bool:
        self.allocated.append((order_id, sku_id, qty))
        return self.add_allocated_result

    async def add_picked(self, order_id: OrderId, sku_id: SkuId, qty: int) -> bool:
        self.picked.append((order_id, sku_id, qty))
        return self.add_picked_result

    async def add_shipped(self, order_id: OrderId, sku_id: SkuId, qty: int) -> bool:
        self.shipped.append((order_id, sku_id, qty))
        return self.add_shipped_result

    async def insert_order(self, order: Order, lines: Sequence[OrderLine]) -> None:
        self.inserted.append((order, list(lines)))


class FakeReservationRepo:
    def __init__(
        self,
        held: list[Reservation] | None = None,
        *,
        transition_result: bool = True,
    ) -> None:
        self.added: list[Reservation] = []
        self.held = held or []
        self.transition_result = transition_result
        self.transitions: list[tuple[ReservationId, ReservationState, ReservationState]] = []

    async def add(self, reservation: Reservation) -> None:
        self.added.append(reservation)

    async def held_for_order(self, order_id: OrderId) -> list[Reservation]:
        return list(self.held)

    async def transition(
        self, reservation_id: ReservationId, expected: ReservationState, new: ReservationState
    ) -> bool:
        self.transitions.append((reservation_id, expected, new))
        return self.transition_result


class FakeMovementRepo:
    def __init__(self) -> None:
        self.appended: list[Movement] = []

    async def append(self, movement: Movement) -> None:
        self.appended.append(movement)


class FakeCatalogRepo:
    def __init__(self, known: set[SkuId] | None = None) -> None:
        self.known = known if known is not None else set()

    async def missing_skus(self, skus: set[SkuId]) -> set[SkuId]:
        return skus - self.known


class FakeIdempotencyRepo:
    def __init__(
        self,
        claim_outcome: ClaimOutcome = ClaimOutcome.CLAIMED,
        stored: StoredResponse | None = None,
    ) -> None:
        self.claim_outcome = claim_outcome
        self.stored = stored
        self.claim_calls: list[tuple[IdempotencyKey, str]] = []
        self.finalize_calls: list[
            tuple[IdempotencyKey, IdempotencyStatus, dict[str, Any] | None]
        ] = []

    async def claim(self, key: IdempotencyKey, fingerprint: str) -> ClaimOutcome:
        self.claim_calls.append((key, fingerprint))
        return self.claim_outcome

    async def load(self, key: IdempotencyKey) -> StoredResponse | None:
        return self.stored

    async def finalize(
        self, key: IdempotencyKey, status: IdempotencyStatus, response: dict[str, Any] | None
    ) -> None:
        self.finalize_calls.append((key, status, response))


class FakeUnitOfWork:
    """A record-only UnitOfWork. Supports being re-entered across retry attempts."""

    def __init__(
        self,
        stock: FakeStockRepo | None = None,
        orders: FakeOrderRepo | None = None,
        reservations: FakeReservationRepo | None = None,
        movements: FakeMovementRepo | None = None,
        idempotency: FakeIdempotencyRepo | None = None,
        catalog: FakeCatalogRepo | None = None,
    ) -> None:
        self.stock: StockRepo = stock or FakeStockRepo()
        self.orders: OrderRepo = orders or FakeOrderRepo()
        self.reservations: ReservationRepo = reservations or FakeReservationRepo()
        self.movements: MovementRepo = movements or FakeMovementRepo()
        self.idempotency: IdempotencyRepo = idempotency or FakeIdempotencyRepo()
        self.catalog: CatalogRepo = catalog or FakeCatalogRepo()
        self.commits = 0
        self.rollbacks = 0
        self.enters = 0

    async def __aenter__(self) -> FakeUnitOfWork:
        self.enters += 1
        return self

    async def __aexit__(self, *exc: object) -> None:
        return None

    async def commit(self) -> None:
        self.commits += 1

    async def rollback(self) -> None:
        self.rollbacks += 1


def fake_factory(uow: FakeUnitOfWork) -> UnitOfWorkFactory:
    """A UnitOfWorkFactory that always returns the given fake (re-entered per attempt)."""

    def factory() -> UnitOfWork:
        return uow

    return factory
