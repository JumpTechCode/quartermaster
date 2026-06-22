"""Build a Deps bundle wrapping the record-only fakes for API unit tests."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

from quartermaster.api.deps import Deps
from quartermaster.domain.ids import MovementId, OrderId, ReceiptId, ReservationId
from tests.unit.fakes import FakeUnitOfWork, fake_factory

_RID = ReservationId(UUID("00000000-0000-7000-8000-000000000002"))
_MID = MovementId(UUID("00000000-0000-7000-8000-000000000003"))
_RCID = ReceiptId(UUID("00000000-0000-7000-8000-000000000004"))
_FIXED = datetime(2026, 6, 18, tzinfo=UTC)


def make_deps(
    uow: FakeUnitOfWork,
    *,
    order_id: OrderId | None = None,
    receipt_id: ReceiptId | None = None,
    read_uow: FakeUnitOfWork | None = None,
) -> Deps:
    oid = order_id or OrderId(UUID("00000000-0000-7000-8000-000000000001"))
    rcid = receipt_id or _RCID
    return Deps(
        uow_factory=fake_factory(uow),
        read_uow_factory=fake_factory(read_uow if read_uow is not None else uow),
        now=lambda: _FIXED,
        new_order_id=lambda: oid,
        new_receipt_id=lambda: rcid,
        new_reservation_id=lambda: _RID,
        new_movement_id=lambda: _MID,
    )
