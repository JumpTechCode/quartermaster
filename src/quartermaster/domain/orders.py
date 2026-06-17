"""Outbound order documents and their lines.

The ``OrderLine`` carries the partiality math: the per-line quantities advance
monotonically through ``ordered → allocated → picked → shipped`` (design spec §7
"state integrity"), enforced at construction and by guarded operations that
mirror the SQL ``WHERE`` guards (§5.2). The ``Order`` record itself is a thin
document carrying its lifecycle state and OCC version; the legality of state
changes lives in ``ORDER_MACHINE``, not here.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime

from quartermaster.domain.errors import InvariantViolation
from quartermaster.domain.ids import OrderId, SkuId
from quartermaster.domain.state_machines import OrderState


def _require_non_negative(qty: int) -> None:
    if qty < 0:
        raise ValueError(f"quantity must be non-negative, got {qty}")


@dataclass(frozen=True)
class OrderLine:
    """One SKU on an order, tracking its monotonic fulfilment quantities."""

    order_id: OrderId
    sku_id: SkuId
    ordered: int
    allocated: int
    picked: int
    shipped: int

    def __post_init__(self) -> None:
        if not (0 <= self.shipped <= self.picked <= self.allocated <= self.ordered):
            raise InvariantViolation(
                "order line quantities must satisfy "
                "0 <= shipped <= picked <= allocated <= ordered, got "
                f"ordered={self.ordered}, allocated={self.allocated}, "
                f"picked={self.picked}, shipped={self.shipped}"
            )

    @property
    def outstanding_to_allocate(self) -> int:
        """Quantity still owed (the backorder amount): ``ordered - allocated``."""
        return self.ordered - self.allocated

    @property
    def outstanding_to_pick(self) -> int:
        """Reserved but not yet picked: ``allocated - picked``."""
        return self.allocated - self.picked

    @property
    def outstanding_to_ship(self) -> int:
        """Picked but not yet shipped: ``picked - shipped``."""
        return self.picked - self.shipped

    @property
    def is_fully_allocated(self) -> bool:
        """Whether the whole ordered quantity is reserved."""
        return self.allocated == self.ordered

    def allocate(self, qty: int) -> OrderLine:
        """Reserve ``qty`` more against this line. Guard: ``allocated + qty <= ordered``."""
        _require_non_negative(qty)
        if self.allocated + qty > self.ordered:
            raise InvariantViolation(
                f"cannot allocate {qty}: would exceed ordered "
                f"({self.allocated} allocated of {self.ordered})"
            )
        return replace(self, allocated=self.allocated + qty)

    def pick(self, qty: int) -> OrderLine:
        """Pick ``qty`` from the reserved quantity. Guard: ``picked + qty <= allocated``."""
        _require_non_negative(qty)
        if self.picked + qty > self.allocated:
            raise InvariantViolation(
                f"cannot pick {qty}: would exceed allocated "
                f"({self.picked} picked of {self.allocated})"
            )
        return replace(self, picked=self.picked + qty)

    def ship(self, qty: int) -> OrderLine:
        """Ship ``qty`` of the picked quantity. Guard: ``shipped + qty <= picked``."""
        _require_non_negative(qty)
        if self.shipped + qty > self.picked:
            raise InvariantViolation(
                f"cannot ship {qty}: would exceed picked ({self.shipped} shipped of {self.picked})"
            )
        return replace(self, shipped=self.shipped + qty)


@dataclass(frozen=True)
class Order:
    """An outbound order document: lifecycle state plus its OCC version."""

    order_id: OrderId
    state: OrderState
    version: int
    created_at: datetime

    def __post_init__(self) -> None:
        if self.version < 1:
            raise InvariantViolation(f"version must be >= 1, got {self.version}")
