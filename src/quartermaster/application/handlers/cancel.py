"""The ``cancel`` command handler and its convenience runner.

Cancel is **release-only**: legal only from ``created``/``allocated``/``backordered``
(the pre-pick states that have yet to take, or still hold, reservations). It CASes
the order to ``cancelled`` and releases every still-held reservation. Each release
is **gated on a reservation-state CAS** (``held → released``): only the winner does
the guarded ``-reserved`` and appends a ``RELEASE`` movement, so a reservation
already finalised by another actor (a future reaper) is a defined no-op, not an
error (design §4). From ``created`` there are no reservations — a pure CAS.
"""

from __future__ import annotations

from collections.abc import Callable

from quartermaster.application.clock import Clock
from quartermaster.application.commands import CancelCommand
from quartermaster.application.envelope import execute
from quartermaster.application.errors import OccConflict
from quartermaster.application.ports import UnitOfWork, UnitOfWorkFactory
from quartermaster.application.results import CancelResult
from quartermaster.domain.errors import InvariantViolation, OrderNotFound
from quartermaster.domain.ids import IdempotencyKey, MovementId, OrderId, ReservationId
from quartermaster.domain.movements import Movement, MovementType
from quartermaster.domain.state_machines import ORDER_MACHINE, OrderState, ReservationState


async def cancel(
    uow: UnitOfWork,
    command: CancelCommand,
    *,
    now: Clock,
    new_movement_id: Callable[[], MovementId],
) -> CancelResult:
    """Cancel the order and release its held reservations."""
    order = await uow.orders.get(command.order_id)
    if order is None:
        raise OrderNotFound(f"order {command.order_id} does not exist")
    ORDER_MACHINE.assert_legal(order.state, OrderState.CANCELLED)

    reservations = await uow.reservations.held_for_order(command.order_id)
    if not await uow.orders.cas_state(
        command.order_id, order.state, order.version, OrderState.CANCELLED
    ):
        raise OccConflict(f"order {command.order_id} changed under cancel")

    released: list[ReservationId] = []
    for res in reservations:
        if not await uow.reservations.transition(
            res.reservation_id, ReservationState.HELD, ReservationState.RELEASED
        ):
            continue  # another actor finalised this reservation; defined no-op (design §4b)
        if not await uow.stock.release(res.sku_id, res.location_id, res.qty):
            raise InvariantViolation(
                f"reservation {res.reservation_id} was held but its stock is missing"
            )
        await uow.movements.append(
            Movement(
                movement_id=new_movement_id(),
                ts=now(),
                type=MovementType.RELEASE,
                sku_id=res.sku_id,
                from_location=res.location_id,
                to_location=None,
                qty=res.qty,
                ref=command.order_id,
                command_id=command.key,
            )
        )
        released.append(res.reservation_id)

    return CancelResult(
        order_id=command.order_id,
        state=OrderState.CANCELLED,
        released_reservation_ids=tuple(released),
    )


async def run_cancel(
    uow_factory: UnitOfWorkFactory,
    order_id: OrderId,
    key: IdempotencyKey,
    *,
    now: Clock,
    new_movement_id: Callable[[], MovementId],
) -> CancelResult:
    """Build the command, bind the handler's seams, and run it through the envelope."""
    command = CancelCommand(order_id, key)

    async def handler(uow: UnitOfWork, cmd: CancelCommand) -> CancelResult:
        return await cancel(uow, cmd, now=now, new_movement_id=new_movement_id)

    return await execute(uow_factory, command, handler, CancelResult.decode)
