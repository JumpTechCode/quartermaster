"""Read-side queries the API serves directly (no envelope, no idempotency).

A read opens a unit of work, reads the order header and lines, and closes the
transaction without committing. Returning a small ``OrderView`` value keeps the
read orchestration in ``application`` and the route a one-liner.
"""

from __future__ import annotations

from dataclasses import dataclass

from quartermaster.application.ports import UnitOfWorkFactory
from quartermaster.domain.ids import OrderId, ReceiptId
from quartermaster.domain.orders import OrderLine
from quartermaster.domain.receipts import ReceiptKind, ReceiptLine
from quartermaster.domain.state_machines import OrderState, ReceiptState


@dataclass(frozen=True)
class OrderView:
    """A read-only snapshot of an order header and its lines."""

    order_id: OrderId
    state: OrderState
    version: int
    lines: tuple[OrderLine, ...]


async def load_order(uow_factory: UnitOfWorkFactory, order_id: OrderId) -> OrderView | None:
    """Read an order header and its lines; ``None`` if the order does not exist."""
    async with uow_factory() as uow:
        order = await uow.orders.get(order_id)
        if order is None:
            return None
        lines = await uow.orders.get_lines(order_id)
        return OrderView(
            order_id=order.order_id,
            state=order.state,
            version=order.version,
            lines=tuple(lines),
        )


@dataclass(frozen=True)
class ReceiptView:
    """A read-only snapshot of a receipt header and its lines."""

    receipt_id: ReceiptId
    kind: ReceiptKind
    state: ReceiptState
    version: int
    lines: tuple[ReceiptLine, ...]


async def load_receipt(uow_factory: UnitOfWorkFactory, receipt_id: ReceiptId) -> ReceiptView | None:
    """Read a receipt header and its lines; ``None`` if the receipt does not exist."""
    async with uow_factory() as uow:
        receipt = await uow.receipts.get(receipt_id)
        if receipt is None:
            return None
        lines = await uow.receipts.get_lines(receipt_id)
        return ReceiptView(
            receipt_id=receipt.receipt_id,
            kind=receipt.kind,
            state=receipt.state,
            version=receipt.version,
            lines=tuple(lines),
        )
