"""The ``create_receipt`` command handler and its convenience runner.

Creation is an uncontended insert, but it still flows through the transaction
envelope so a retried request replays one receipt (one server-generated id)
rather than creating duplicates. The handler validates that every line's SKU
exists in the catalog before inserting; an unknown SKU is a hard rejection.
This slice creates supplier receipts only (design spec §2); RMAs are deferred.
Time and id generation enter via injected seams.
"""

from __future__ import annotations

from collections.abc import Callable

from quartermaster.application.clock import Clock
from quartermaster.application.commands import CreateReceiptCommand
from quartermaster.application.envelope import execute
from quartermaster.application.ports import UnitOfWork, UnitOfWorkFactory
from quartermaster.application.results import CreatedReceiptLine, CreateReceiptResult
from quartermaster.domain.errors import UnknownSku
from quartermaster.domain.ids import IdempotencyKey, ReceiptId, SkuId
from quartermaster.domain.receipts import Receipt, ReceiptKind, ReceiptLine
from quartermaster.domain.state_machines import ReceiptState


async def create_receipt(
    uow: UnitOfWork,
    command: CreateReceiptCommand,
    *,
    now: Clock,
    new_receipt_id: Callable[[], ReceiptId],
) -> CreateReceiptResult:
    """Create a new supplier receipt (header + lines) in the ``expected`` state."""
    skus = {sku for sku, _ in command.lines}
    missing = await uow.catalog.missing_skus(skus)
    if missing:
        listed = ", ".join(sorted(missing))
        raise UnknownSku(f"unknown sku(s): {listed}")

    receipt_id = new_receipt_id()
    receipt = Receipt(
        receipt_id=receipt_id,
        kind=ReceiptKind.SUPPLIER_RECEIPT,
        state=ReceiptState.EXPECTED,
        version=1,
        created_at=now(),
        origin_order_id=None,
    )
    lines = [
        ReceiptLine(receipt_id=receipt_id, sku_id=sku, expected=qty, received=0)
        for sku, qty in command.lines
    ]
    await uow.receipts.insert_receipt(receipt, lines)
    return CreateReceiptResult(
        receipt_id=receipt_id,
        kind=ReceiptKind.SUPPLIER_RECEIPT,
        state=ReceiptState.EXPECTED,
        lines=tuple(CreatedReceiptLine(sku, qty) for sku, qty in command.lines),
    )


async def run_create_receipt(
    uow_factory: UnitOfWorkFactory,
    lines: tuple[tuple[SkuId, int], ...],
    key: IdempotencyKey,
    *,
    now: Clock,
    new_receipt_id: Callable[[], ReceiptId],
) -> CreateReceiptResult:
    """Build the command, bind the handler's seams, and run it through the envelope."""
    command = CreateReceiptCommand(lines, key)

    async def handler(uow: UnitOfWork, cmd: CreateReceiptCommand) -> CreateReceiptResult:
        return await create_receipt(uow, cmd, now=now, new_receipt_id=new_receipt_id)

    return await execute(uow_factory, command, handler, CreateReceiptResult.decode)
