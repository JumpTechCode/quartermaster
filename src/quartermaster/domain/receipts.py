"""Inbound receipt documents and their lines.

A receipt is the inbound counterpart to an order. Its lines track expected vs.
received quantities, allowing partial receipts (short shipments): ``received``
never exceeds ``expected`` but may fall short. The single Receipt lifecycle
serves both supplier receipts and customer returns; an RMA is a receipt whose
``kind`` is ``CUSTOMER_RMA`` and which references the order it returns (design
spec §3, §4).
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime
from enum import StrEnum

from quartermaster.domain.errors import InvariantViolation
from quartermaster.domain.ids import OrderId, ReceiptId, SkuId
from quartermaster.domain.quantities import MAX_QTY
from quartermaster.domain.state_machines import ReceiptState


class ReceiptKind(StrEnum):
    """Whether a receipt is an inbound supply or a customer return."""

    SUPPLIER_RECEIPT = "supplier_receipt"
    CUSTOMER_RMA = "customer_rma"


def _require_non_negative(qty: int) -> None:
    if qty < 0:
        raise ValueError(f"quantity must be non-negative, got {qty}")


@dataclass(frozen=True)
class ReceiptLine:
    """One SKU on a receipt, tracking expected vs. received (allows shortfall)."""

    receipt_id: ReceiptId
    sku_id: SkuId
    expected: int
    received: int

    def __post_init__(self) -> None:
        if not (0 <= self.received <= self.expected):
            raise InvariantViolation(
                "receipt line must satisfy 0 <= received <= expected, got "
                f"expected={self.expected}, received={self.received}"
            )
        if self.expected > MAX_QTY:
            raise InvariantViolation(
                f"receipt line quantity must not exceed {MAX_QTY} "
                f"(the 32-bit column ceiling), got expected={self.expected}"
            )

    @property
    def shortfall(self) -> int:
        """Quantity still missing on a short shipment: ``expected - received``."""
        return self.expected - self.received

    @property
    def is_complete(self) -> bool:
        """Whether the full expected quantity has been received."""
        return self.received == self.expected

    def receive(self, qty: int) -> ReceiptLine:
        """Record ``qty`` more received. Guard: ``received + qty <= expected``."""
        _require_non_negative(qty)
        if self.received + qty > self.expected:
            raise InvariantViolation(
                f"cannot receive {qty}: would exceed expected "
                f"({self.received} received of {self.expected})"
            )
        return replace(self, received=self.received + qty)


@dataclass(frozen=True)
class Receipt:
    """An inbound receipt document (supplier receipt or customer RMA)."""

    receipt_id: ReceiptId
    kind: ReceiptKind
    state: ReceiptState
    version: int
    created_at: datetime
    origin_order_id: OrderId | None

    def __post_init__(self) -> None:
        if self.version < 1:
            raise InvariantViolation(f"version must be >= 1, got {self.version}")
        is_rma = self.kind == ReceiptKind.CUSTOMER_RMA
        has_origin = self.origin_order_id is not None
        if is_rma != has_origin:
            raise InvariantViolation(
                "origin_order_id must be set iff kind is customer_rma, got "
                f"kind={self.kind.value}, origin_order_id={self.origin_order_id}"
            )
