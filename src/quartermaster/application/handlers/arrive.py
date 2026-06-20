"""The ``arrive`` command handler and its convenience runner.

A pure document transition: ``expected -> arrived`` via a state/version CAS.
The truck is at the dock but nothing has been counted yet, so arrive touches no
stock and appends no movement — it is a lifecycle gate (design spec §2),
mirroring the outbound ``pack``.
"""

from __future__ import annotations

from quartermaster.application.commands import ArriveCommand
from quartermaster.application.envelope import execute
from quartermaster.application.errors import OccConflict
from quartermaster.application.ports import UnitOfWork, UnitOfWorkFactory
from quartermaster.application.results import ArriveResult
from quartermaster.domain.errors import ReceiptNotFound
from quartermaster.domain.ids import IdempotencyKey, ReceiptId
from quartermaster.domain.state_machines import RECEIPT_MACHINE, ReceiptState


async def arrive(uow: UnitOfWork, command: ArriveCommand) -> ArriveResult:
    """Advance the receipt ``expected -> arrived``."""
    receipt = await uow.receipts.get(command.receipt_id)
    if receipt is None:
        raise ReceiptNotFound(f"receipt {command.receipt_id} does not exist")
    RECEIPT_MACHINE.assert_legal(receipt.state, ReceiptState.ARRIVED)
    if not await uow.receipts.cas_state(
        command.receipt_id, receipt.state, receipt.version, ReceiptState.ARRIVED
    ):
        raise OccConflict(f"receipt {command.receipt_id} changed under arrive")
    return ArriveResult(receipt_id=command.receipt_id, state=ReceiptState.ARRIVED)


async def run_arrive(
    uow_factory: UnitOfWorkFactory, receipt_id: ReceiptId, key: IdempotencyKey
) -> ArriveResult:
    """Build the command and run it through the envelope."""
    command = ArriveCommand(receipt_id, key)

    async def handler(uow: UnitOfWork, cmd: ArriveCommand) -> ArriveResult:
        return await arrive(uow, cmd)

    return await execute(uow_factory, command, handler, ArriveResult.decode)
