"""The append-only movement ledger record.

Every stock state change appends a ``Movement``. The ledger is the audit and
conservation-oracle source (design spec §3, §7): the offline oracle sums only the
on-hand-affecting types — ``RECEIVE`` (+), ``PICK`` (-), and ``PUTAWAY``
(net-zero relocation between locations) — while ``RESERVE``/``RELEASE`` touch
``reserved``, not on-hand. It is never summed on the command path. The detailed
type-to-location rules are enforced where commands build movements (a later
slice); here the record carries the faithful shape and the one structural
invariant that always holds: a movement moves a positive quantity.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

from quartermaster.domain.errors import InvariantViolation
from quartermaster.domain.ids import (
    IdempotencyKey,
    LocationId,
    MovementId,
    OrderId,
    ReceiptId,
    SkuId,
)


class MovementType(StrEnum):
    """The kinds of stock state change recorded in the ledger."""

    RECEIVE = "receive"
    PUTAWAY = "putaway"
    PICK = "pick"
    RESERVE = "reserve"
    RELEASE = "release"


@dataclass(frozen=True)
class Movement:
    """One append-only ledger entry for a stock state change."""

    movement_id: MovementId
    ts: datetime
    type: MovementType
    sku_id: SkuId
    from_location: LocationId | None
    to_location: LocationId | None
    qty: int
    ref: OrderId | ReceiptId
    command_id: IdempotencyKey

    def __post_init__(self) -> None:
        if self.qty <= 0:
            raise InvariantViolation(f"movement qty must be > 0, got {self.qty}")
