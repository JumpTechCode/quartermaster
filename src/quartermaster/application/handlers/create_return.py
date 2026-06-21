"""The ``create_return`` command handler and its convenience runner.

A return is an inbound customer RMA: a Receipt whose ``kind`` is ``CUSTOMER_RMA``
referencing the order it returns (ADR-0008). Creation validates the return
against the origin order — the order must exist and be ``SHIPPED``, and each
return line's SKU must have been shipped on that order in at least the returned
quantity (ADR-0022). Validation is per-line and non-cumulative: the engine does
not track quantities returned across multiple RMAs. Like ``create_receipt`` the
insert is uncontended but still flows through the envelope so a retried request
replays one receipt (one server-generated id). Time and id generation enter via
injected seams.
"""

from __future__ import annotations

from collections.abc import Callable

from quartermaster.application.clock import Clock
from quartermaster.application.commands import CreateReturnCommand
from quartermaster.application.envelope import execute
from quartermaster.application.ports import UnitOfWork, UnitOfWorkFactory
from quartermaster.application.results import CreatedReceiptLine, CreateReceiptResult
from quartermaster.domain.errors import OrderNotFound, ReturnNotAllowed
from quartermaster.domain.ids import IdempotencyKey, OrderId, ReceiptId, SkuId
from quartermaster.domain.receipts import Receipt, ReceiptKind, ReceiptLine
from quartermaster.domain.state_machines import OrderState, ReceiptState


async def create_return(
    uow: UnitOfWork,
    command: CreateReturnCommand,
    *,
    now: Clock,
    new_receipt_id: Callable[[], ReceiptId],
) -> CreateReceiptResult:
    """Create a customer-RMA receipt for goods returned against a shipped order."""
    order = await uow.orders.get(command.order_id)
    if order is None:
        raise OrderNotFound(f"order {command.order_id} does not exist")
    if order.state is not OrderState.SHIPPED:
        raise ReturnNotAllowed(
            f"order {command.order_id} is {order.state.value}, not shipped; cannot return"
        )

    shipped = {line.sku_id: line.shipped for line in await uow.orders.get_lines(command.order_id)}
    for sku, qty in command.lines:
        available = shipped.get(sku, 0)
        if available == 0:
            raise ReturnNotAllowed(f"sku {sku} was not shipped on order {command.order_id}")
        if qty > available:
            raise ReturnNotAllowed(
                f"cannot return {qty} of {sku}: only {available} shipped on "
                f"order {command.order_id}"
            )

    receipt_id = new_receipt_id()
    receipt = Receipt(
        receipt_id=receipt_id,
        kind=ReceiptKind.CUSTOMER_RMA,
        state=ReceiptState.EXPECTED,
        version=1,
        created_at=now(),
        origin_order_id=command.order_id,
    )
    lines = [
        ReceiptLine(receipt_id=receipt_id, sku_id=sku, expected=qty, received=0)
        for sku, qty in command.lines
    ]
    await uow.receipts.insert_receipt(receipt, lines)
    return CreateReceiptResult(
        receipt_id=receipt_id,
        kind=ReceiptKind.CUSTOMER_RMA,
        state=ReceiptState.EXPECTED,
        lines=tuple(CreatedReceiptLine(sku, qty) for sku, qty in command.lines),
    )


async def run_create_return(
    uow_factory: UnitOfWorkFactory,
    order_id: OrderId,
    lines: tuple[tuple[SkuId, int], ...],
    key: IdempotencyKey,
    *,
    now: Clock,
    new_receipt_id: Callable[[], ReceiptId],
) -> CreateReceiptResult:
    """Build the command, bind the handler's seams, and run it through the envelope."""
    command = CreateReturnCommand(order_id, lines, key)

    async def handler(uow: UnitOfWork, cmd: CreateReturnCommand) -> CreateReceiptResult:
        return await create_return(uow, cmd, now=now, new_receipt_id=new_receipt_id)

    return await execute(uow_factory, command, handler, CreateReceiptResult.decode)
