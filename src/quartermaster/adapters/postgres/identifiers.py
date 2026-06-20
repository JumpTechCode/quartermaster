"""App-side UUIDv7 generation for synthetic document identifiers.

Document ids (order/receipt/reservation/movement) are time-ordered UUIDv7 so
inserts land near the right edge of the primary-key B-tree — better index
locality and less fragmentation on the high-write, append-only movement ledger
(design spec §6). Python 3.13's stdlib ships only uuid1/3/4/5, so the v7 layout
comes from ``uuid-utils``; the value is returned as a stdlib :class:`uuid.UUID`
so the rest of the code stays on the standard type. The id is minted before the
insert so the command can reference the document from its movement rows inside
the same transaction.
"""

from __future__ import annotations

from uuid import UUID

import uuid_utils

from quartermaster.domain.ids import MovementId, OrderId, ReceiptId, ReservationId


def new_uuid7() -> UUID:
    """Return a fresh time-ordered UUIDv7 as a stdlib :class:`uuid.UUID`."""
    return UUID(int=uuid_utils.uuid7().int)


def new_order_id() -> OrderId:
    """Mint a fresh order identifier."""
    return OrderId(new_uuid7())


def new_reservation_id() -> ReservationId:
    """Mint a fresh reservation identifier."""
    return ReservationId(new_uuid7())


def new_movement_id() -> MovementId:
    """Mint a fresh movement (ledger entry) identifier."""
    return MovementId(new_uuid7())


def new_receipt_id() -> ReceiptId:
    """Mint a fresh receipt identifier."""
    return ReceiptId(new_uuid7())
