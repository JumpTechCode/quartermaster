"""The ``putaway`` command handler and its convenience runner.

Relocates a received receipt's stock from its receiving location to a shelf,
advancing ``received -> putaway_complete`` in one transaction. The document-state
CAS (``received -> putaway_complete``) is the concurrency gate — exactly as receive
gates on ``arrived -> receiving`` — so the per-line stock moves beneath it are
uncontended for this receipt. Each line lowers on-hand at the receiving cell
(guarded so only unreserved stock moves) and raises it at the shelf, appending a
``PUTAWAY`` movement. Putaway is inbound-only: it never re-allocates backordered
orders — an async sweep does that (design spec §1). Time and id generation enter
via injected seams.
"""

from __future__ import annotations

from collections.abc import Callable

from quartermaster.application.clock import Clock
from quartermaster.application.commands import PutawayCommand
from quartermaster.application.envelope import execute
from quartermaster.application.errors import OccConflict
from quartermaster.application.ports import UnitOfWork, UnitOfWorkFactory
from quartermaster.application.results import PutawayLine, PutawayResult
from quartermaster.domain.errors import InvariantViolation, ReceiptNotFound, UnknownLocation
from quartermaster.domain.ids import IdempotencyKey, LocationId, MovementId, ReceiptId
from quartermaster.domain.movements import Movement, MovementType
from quartermaster.domain.state_machines import RECEIPT_MACHINE, ReceiptState


async def putaway(
    uow: UnitOfWork,
    command: PutawayCommand,
    *,
    now: Clock,
    new_movement_id: Callable[[], MovementId],
) -> PutawayResult:
    """Relocate the receipt's received stock from receiving to the shelf."""
    receipt = await uow.receipts.get(command.receipt_id)
    if receipt is None:
        raise ReceiptNotFound(f"receipt {command.receipt_id} does not exist")
    RECEIPT_MACHINE.assert_legal(receipt.state, ReceiptState.PUTAWAY_COMPLETE)

    if not await uow.catalog.location_exists(command.from_location):
        raise UnknownLocation(f"unknown location: {command.from_location}")
    if not await uow.catalog.location_exists(command.to_location):
        raise UnknownLocation(f"unknown location: {command.to_location}")

    lines = await uow.receipts.get_lines(command.receipt_id)
    if not await uow.receipts.cas_state(
        command.receipt_id, receipt.state, receipt.version, ReceiptState.PUTAWAY_COMPLETE
    ):
        raise OccConflict(f"receipt {command.receipt_id} changed under putaway")

    moved: list[PutawayLine] = []
    for line in lines:
        if line.received == 0:
            continue
        if not await uow.stock.remove_on_hand(line.sku_id, command.from_location, line.received):
            raise InvariantViolation(
                f"receipt {command.receipt_id} line {line.sku_id}: "
                f"{line.received} not available at {command.from_location}"
            )
        await uow.stock.add_on_hand(line.sku_id, command.to_location, line.received)
        await uow.movements.append(
            Movement(
                movement_id=new_movement_id(),
                ts=now(),
                type=MovementType.PUTAWAY,
                sku_id=line.sku_id,
                from_location=command.from_location,
                to_location=command.to_location,
                qty=line.received,
                ref=command.receipt_id,
                command_id=command.key,
            )
        )
        moved.append(PutawayLine(line.sku_id, line.received))

    return PutawayResult(
        receipt_id=command.receipt_id,
        state=ReceiptState.PUTAWAY_COMPLETE,
        lines=tuple(moved),
    )


async def run_putaway(
    uow_factory: UnitOfWorkFactory,
    receipt_id: ReceiptId,
    from_location: LocationId,
    to_location: LocationId,
    key: IdempotencyKey,
    *,
    now: Clock,
    new_movement_id: Callable[[], MovementId],
) -> PutawayResult:
    """Build the command, bind the handler's seams, and run it through the envelope."""
    command = PutawayCommand(receipt_id, from_location, to_location, key)

    async def handler(uow: UnitOfWork, cmd: PutawayCommand) -> PutawayResult:
        return await putaway(uow, cmd, now=now, new_movement_id=new_movement_id)

    return await execute(uow_factory, command, handler, PutawayResult.decode)
