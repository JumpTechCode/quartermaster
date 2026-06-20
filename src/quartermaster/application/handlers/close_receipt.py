"""The ``close`` command handler and its convenience runner.

A pure document transition ``putaway_complete -> closed`` via a state/version CAS —
the lifecycle terminator for a receipt. No stock, no movement (design spec §2),
mirroring the outbound ``pack``.
"""

from __future__ import annotations

from quartermaster.application.commands import CloseReceiptCommand
from quartermaster.application.envelope import execute
from quartermaster.application.errors import OccConflict
from quartermaster.application.ports import UnitOfWork, UnitOfWorkFactory
from quartermaster.application.results import CloseReceiptResult
from quartermaster.domain.errors import ReceiptNotFound
from quartermaster.domain.ids import IdempotencyKey, ReceiptId
from quartermaster.domain.state_machines import RECEIPT_MACHINE, ReceiptState


async def close_receipt(uow: UnitOfWork, command: CloseReceiptCommand) -> CloseReceiptResult:
    """Advance the receipt ``putaway_complete -> closed``."""
    receipt = await uow.receipts.get(command.receipt_id)
    if receipt is None:
        raise ReceiptNotFound(f"receipt {command.receipt_id} does not exist")
    RECEIPT_MACHINE.assert_legal(receipt.state, ReceiptState.CLOSED)
    if not await uow.receipts.cas_state(
        command.receipt_id, receipt.state, receipt.version, ReceiptState.CLOSED
    ):
        raise OccConflict(f"receipt {command.receipt_id} changed under close")
    return CloseReceiptResult(receipt_id=command.receipt_id, state=ReceiptState.CLOSED)


async def run_close_receipt(
    uow_factory: UnitOfWorkFactory, receipt_id: ReceiptId, key: IdempotencyKey
) -> CloseReceiptResult:
    """Build the command and run it through the envelope."""
    command = CloseReceiptCommand(receipt_id, key)

    async def handler(uow: UnitOfWork, cmd: CloseReceiptCommand) -> CloseReceiptResult:
        return await close_receipt(uow, cmd)

    return await execute(uow_factory, command, handler, CloseReceiptResult.decode)
