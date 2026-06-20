"""Unit tests for the centralized error handlers (errors.py).

The catch-all 500 handler is tested by injecting a uow_factory whose UoW raises
an unmapped RuntimeError when the read route calls it, then asserting the response
is a shaped 500 rather than a raw exception.

Note: httpx.ASGITransport must be constructed with raise_app_exceptions=False
when testing 500 responses. Starlette's ServerErrorMiddleware always re-raises
the exception after sending the shaped response, so ASGITransport's default
raise_app_exceptions=True would propagate the RuntimeError into the test.
Using raise_app_exceptions=False is the documented httpx pattern for this case.
"""

from __future__ import annotations

import datetime
from collections.abc import Sequence
from uuid import UUID

import httpx

from quartermaster.api.app import create_app
from quartermaster.api.deps import Deps
from quartermaster.application.ports import (
    CatalogRepo,
    ClaimOutcome,
    IdempotencyRepo,
    MovementRepo,
    OrderRepo,
    ReceiptRepo,
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
    MovementId,
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

_OID = OrderId(UUID("00000000-0000-7000-8000-000000000001"))
_RID = ReservationId(UUID("00000000-0000-7000-8000-000000000002"))
_MID = MovementId(UUID("00000000-0000-7000-8000-000000000003"))
_RCID = ReceiptId(UUID("00000000-0000-7000-8000-000000000004"))
_FIXED = datetime.datetime(2026, 6, 18, tzinfo=datetime.UTC)


# ---------------------------------------------------------------------------
# Minimal no-op repo stubs satisfying each Protocol
# ---------------------------------------------------------------------------


class _NoopStockRepo:
    async def stock_locations(self, sku: SkuId) -> list[tuple[LocationId, int]]:  # pragma: no cover
        return []

    async def reserve_up_to(
        self, sku: SkuId, location: LocationId, want: int
    ) -> int:  # pragma: no cover
        return 0

    async def consume(self, sku: SkuId, location: LocationId, qty: int) -> bool:  # pragma: no cover
        return True

    async def release(self, sku: SkuId, location: LocationId, qty: int) -> bool:  # pragma: no cover
        return True

    async def add_on_hand(
        self, sku: SkuId, location: LocationId, qty: int
    ) -> None:  # pragma: no cover
        pass


class _BoomOrderRepo:
    """An OrderRepo stub whose every method raises an unmapped RuntimeError."""

    async def get(self, order_id: OrderId) -> Order | None:
        raise RuntimeError("database on fire")

    async def get_lines(self, order_id: OrderId) -> list[OrderLine]:  # pragma: no cover
        raise RuntimeError("database on fire")

    async def cas_state(
        self,
        order_id: OrderId,
        expected_state: OrderState,
        expected_version: int,
        new_state: OrderState,
    ) -> bool:  # pragma: no cover
        raise RuntimeError("database on fire")

    async def add_allocated(
        self, order_id: OrderId, sku_id: SkuId, qty: int
    ) -> bool:  # pragma: no cover
        raise RuntimeError("database on fire")

    async def add_picked(
        self, order_id: OrderId, sku_id: SkuId, qty: int
    ) -> bool:  # pragma: no cover
        raise RuntimeError("database on fire")

    async def add_shipped(
        self, order_id: OrderId, sku_id: SkuId, qty: int
    ) -> bool:  # pragma: no cover
        raise RuntimeError("database on fire")

    async def insert_order(
        self, order: Order, lines: Sequence[OrderLine]
    ) -> None:  # pragma: no cover
        raise RuntimeError("database on fire")


class _NoopReservationRepo:
    async def add(self, reservation: Reservation) -> None:  # pragma: no cover
        pass

    async def held_for_order(self, order_id: OrderId) -> list[Reservation]:  # pragma: no cover
        return []

    async def transition(
        self,
        reservation_id: ReservationId,
        expected: ReservationState,
        new: ReservationState,
    ) -> bool:  # pragma: no cover
        return True


class _NoopMovementRepo:
    async def append(self, movement: Movement) -> None:  # pragma: no cover
        pass


class _NoopReceiptRepo:
    async def get(self, receipt_id: ReceiptId) -> Receipt | None:  # pragma: no cover
        return None

    async def get_lines(self, receipt_id: ReceiptId) -> list[ReceiptLine]:  # pragma: no cover
        return []

    async def insert_receipt(
        self, receipt: Receipt, lines: Sequence[ReceiptLine]
    ) -> None:  # pragma: no cover
        pass

    async def cas_state(
        self,
        receipt_id: ReceiptId,
        expected_state: ReceiptState,
        expected_version: int,
        new_state: ReceiptState,
    ) -> bool:  # pragma: no cover
        return True

    async def add_received(
        self, receipt_id: ReceiptId, sku_id: SkuId, qty: int
    ) -> bool:  # pragma: no cover
        return True


class _NoopCatalogRepo:
    async def missing_skus(self, skus: set[SkuId]) -> set[SkuId]:  # pragma: no cover
        return set()

    async def location_exists(self, location: LocationId) -> bool:  # pragma: no cover
        return True


class _NoopIdempotencyRepo:
    async def claim(
        self, key: IdempotencyKey, fingerprint: str
    ) -> ClaimOutcome:  # pragma: no cover
        return ClaimOutcome.CLAIMED

    async def load(self, key: IdempotencyKey) -> StoredResponse | None:  # pragma: no cover
        return None

    async def finalize(
        self,
        key: IdempotencyKey,
        status: IdempotencyStatus,
        response: dict[str, object] | None,
    ) -> None:  # pragma: no cover
        pass


# ---------------------------------------------------------------------------
# UoW that wires the boom repo
# ---------------------------------------------------------------------------


class _BoomUnitOfWork:
    """A UnitOfWork that raises RuntimeError from the orders repo."""

    def __init__(self) -> None:
        self.stock: StockRepo = _NoopStockRepo()
        self.orders: OrderRepo = _BoomOrderRepo()
        self.receipts: ReceiptRepo = _NoopReceiptRepo()
        self.reservations: ReservationRepo = _NoopReservationRepo()
        self.movements: MovementRepo = _NoopMovementRepo()
        self.idempotency: IdempotencyRepo = _NoopIdempotencyRepo()
        self.catalog: CatalogRepo = _NoopCatalogRepo()

    async def __aenter__(self) -> _BoomUnitOfWork:
        return self

    async def __aexit__(self, *exc: object) -> None:
        pass

    async def commit(self) -> None:  # pragma: no cover
        pass

    async def rollback(self) -> None:  # pragma: no cover
        pass


def _boom_uow_factory() -> UnitOfWorkFactory:
    uow: UnitOfWork = _BoomUnitOfWork()

    def factory() -> UnitOfWork:
        return uow

    return factory


def _boom_client() -> httpx.AsyncClient:
    deps = Deps(
        uow_factory=_boom_uow_factory(),
        now=lambda: _FIXED,
        new_order_id=lambda: _OID,
        new_receipt_id=lambda: _RCID,
        new_reservation_id=lambda: _RID,
        new_movement_id=lambda: _MID,
    )
    app = create_app(deps)
    # raise_app_exceptions=False is required: Starlette's ServerErrorMiddleware sends
    # the shaped JSON response then re-raises the exception. ASGITransport's default
    # raise_app_exceptions=True would propagate that re-raise into the test call.
    # Setting it False is the documented httpx use-case for inspecting 500 bodies.
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app, raise_app_exceptions=False),
        base_url="http://t",
    )


async def test_unmapped_exception_returns_shaped_500() -> None:
    """A RuntimeError (not in the domain error map) must yield a uniform shaped 500."""
    async with _boom_client() as client:
        resp = await client.get(f"/orders/{_OID}")
    assert resp.status_code == 500
    assert resp.json() == {"error": "internal_error", "detail": "an unexpected error occurred"}
