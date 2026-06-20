"""The ``receive`` command handler and its convenience runner.

Records the actually-received quantities for an ``arrived`` receipt and lands
that stock at one receiving location, advancing the document
``arrived -> receiving -> received`` in one transaction. The document-state CAS
(``arrived -> receiving``) is the concurrency gate — exactly as the outbound
``pick`` gates on ``allocated -> picking`` — so the per-line writes beneath it are
uncontended for this receipt (design spec §3). Each line raises on-hand at the
cell (an UPSERT, since the cell may be new) and appends a ``RECEIVE`` movement.
Short shipments fall out of line-level partiality: a line received below expected,
or omitted, records the shortfall and the receipt still reaches ``received``.
Time and id generation enter via injected seams.
"""

from __future__ import annotations

from collections.abc import Callable

from quartermaster.application.clock import Clock
from quartermaster.application.commands import ReceiveCommand
from quartermaster.application.envelope import execute
from quartermaster.application.errors import OccConflict
from quartermaster.application.ports import UnitOfWork, UnitOfWorkFactory
from quartermaster.application.results import ReceivedLine, ReceiveResult
from quartermaster.domain.errors import (
    InvalidReceiptLine,
    InvariantViolation,
    ReceiptNotFound,
    UnknownLocation,
)
from quartermaster.domain.ids import IdempotencyKey, LocationId, MovementId, ReceiptId, SkuId
from quartermaster.domain.movements import Movement, MovementType
from quartermaster.domain.state_machines import RECEIPT_MACHINE, ReceiptState


async def receive(
    uow: UnitOfWork,
    command: ReceiveCommand,
    *,
    now: Clock,
    new_movement_id: Callable[[], MovementId],
) -> ReceiveResult:
    """Land the received quantities at the location and advance to ``received``."""
    receipt = await uow.receipts.get(command.receipt_id)
    if receipt is None:
        raise ReceiptNotFound(f"receipt {command.receipt_id} does not exist")
    RECEIPT_MACHINE.assert_legal(receipt.state, ReceiptState.RECEIVING)

    if not await uow.catalog.location_exists(command.location_id):
        raise UnknownLocation(f"unknown location: {command.location_id}")

    lines_by_sku = {line.sku_id: line for line in await uow.receipts.get_lines(command.receipt_id)}
    # Pre-validate every provided line against the receipt before any write
    # (deterministic hard rejections — cached by the envelope). This check is
    # advisory: the authoritative guard is add_received's in-gate WHERE
    # (received_qty + qty <= expected_qty), which runs under the document CAS
    # below where this receipt is single-writer. A stale pre-read that slipped a
    # bad quantity through would simply fail that guard → InvariantViolation.
    for sku, qty in command.lines:
        line = lines_by_sku.get(sku)
        if line is None:
            raise InvalidReceiptLine(f"sku {sku} is not a line on receipt {command.receipt_id}")
        if line.received + qty > line.expected:
            raise InvalidReceiptLine(
                f"receiving {qty} of {sku} exceeds expected "
                f"({line.received} of {line.expected} already received)"
            )

    if not await uow.receipts.cas_state(
        command.receipt_id, receipt.state, receipt.version, ReceiptState.RECEIVING
    ):
        raise OccConflict(f"receipt {command.receipt_id} changed under receive")

    received: dict[SkuId, int] = {}
    for sku, qty in command.lines:
        if not await uow.receipts.add_received(command.receipt_id, sku, qty):
            raise InvariantViolation(
                f"receipt {command.receipt_id} line {sku} guard rejected received += {qty}"
            )
        await uow.stock.add_on_hand(sku, command.location_id, qty)
        await uow.movements.append(
            Movement(
                movement_id=new_movement_id(),
                ts=now(),
                type=MovementType.RECEIVE,
                sku_id=sku,
                from_location=None,
                to_location=command.location_id,
                qty=qty,
                ref=command.receipt_id,
                command_id=command.key,
            )
        )
        received[sku] = received.get(sku, 0) + qty

    RECEIPT_MACHINE.assert_legal(ReceiptState.RECEIVING, ReceiptState.RECEIVED)
    if not await uow.receipts.cas_state(
        command.receipt_id, ReceiptState.RECEIVING, receipt.version + 1, ReceiptState.RECEIVED
    ):
        raise OccConflict(f"receipt {command.receipt_id} changed under receive")

    return ReceiveResult(
        receipt_id=command.receipt_id,
        state=ReceiptState.RECEIVED,
        lines=tuple(ReceivedLine(sku, qty) for sku, qty in received.items()),
    )


async def run_receive(
    uow_factory: UnitOfWorkFactory,
    receipt_id: ReceiptId,
    location_id: LocationId,
    lines: tuple[tuple[SkuId, int], ...],
    key: IdempotencyKey,
    *,
    now: Clock,
    new_movement_id: Callable[[], MovementId],
) -> ReceiveResult:
    """Build the command, bind the handler's seams, and run it through the envelope."""
    command = ReceiveCommand(receipt_id, location_id, lines, key)

    async def handler(uow: UnitOfWork, cmd: ReceiveCommand) -> ReceiveResult:
        return await receive(uow, cmd, now=now, new_movement_id=new_movement_id)

    return await execute(uow_factory, command, handler, ReceiveResult.decode)
