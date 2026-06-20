"""The ``pick`` command handler and its convenience runner.

Consumes every ``held`` reservation of an ``allocated`` order — lowering both
``on_hand`` and ``reserved`` — and advances the order ``allocated → picking →
picked`` in one transaction. Each per-reservation stock change is **gated on a
reservation-state CAS** (``held → consumed``): only the winner mutates stock and
appends a ``PICK`` movement, so a reservation already finalised by another actor
(a future reaper) is a defined no-op, not an error (design §4). The handler is
pure orchestration over the ports; time and id generation enter via injected
callables.
"""

from __future__ import annotations

from collections.abc import Callable

from quartermaster.application.clock import Clock
from quartermaster.application.commands import PickCommand
from quartermaster.application.envelope import execute
from quartermaster.application.errors import OccConflict
from quartermaster.application.ports import UnitOfWork, UnitOfWorkFactory
from quartermaster.application.results import PickedLine, PickResult
from quartermaster.domain.errors import InvariantViolation, OrderNotFound
from quartermaster.domain.ids import IdempotencyKey, MovementId, OrderId, SkuId
from quartermaster.domain.movements import Movement, MovementType
from quartermaster.domain.state_machines import ORDER_MACHINE, OrderState, ReservationState


async def pick(
    uow: UnitOfWork,
    command: PickCommand,
    *,
    now: Clock,
    new_movement_id: Callable[[], MovementId],
) -> PickResult:
    """Consume the order's held reservations and advance it to ``picked``."""
    order = await uow.orders.get(command.order_id)
    if order is None:
        raise OrderNotFound(f"order {command.order_id} does not exist")
    ORDER_MACHINE.assert_legal(order.state, OrderState.PICKING)

    reservations = await uow.reservations.held_for_order(command.order_id)
    if not await uow.orders.cas_state(
        command.order_id, order.state, order.version, OrderState.PICKING
    ):
        raise OccConflict(f"order {command.order_id} changed under pick")

    picked: dict[SkuId, int] = {}
    for res in reservations:
        if not await uow.reservations.transition(
            res.reservation_id, ReservationState.HELD, ReservationState.CONSUMED
        ):
            continue  # another actor finalised this reservation; defined no-op (design §4b)
        if not await uow.stock.consume(res.sku_id, res.location_id, res.qty):
            raise InvariantViolation(
                f"reservation {res.reservation_id} was held but its stock is missing"
            )
        if not await uow.orders.add_picked(command.order_id, res.sku_id, res.qty):
            raise OccConflict(f"order {command.order_id} line {res.sku_id} changed under pick")
        await uow.movements.append(
            Movement(
                movement_id=new_movement_id(),
                ts=now(),
                type=MovementType.PICK,
                sku_id=res.sku_id,
                from_location=res.location_id,
                to_location=None,
                qty=res.qty,
                ref=command.order_id,
                command_id=command.key,
            )
        )
        picked[res.sku_id] = picked.get(res.sku_id, 0) + res.qty

    ORDER_MACHINE.assert_legal(OrderState.PICKING, OrderState.PICKED)
    if not await uow.orders.cas_state(
        command.order_id, OrderState.PICKING, order.version + 1, OrderState.PICKED
    ):
        raise OccConflict(f"order {command.order_id} changed under pick")

    return PickResult(
        order_id=command.order_id,
        state=OrderState.PICKED,
        lines=tuple(PickedLine(sku, qty) for sku, qty in picked.items()),
    )


async def run_pick(
    uow_factory: UnitOfWorkFactory,
    order_id: OrderId,
    key: IdempotencyKey,
    *,
    now: Clock,
    new_movement_id: Callable[[], MovementId],
) -> PickResult:
    """Build the command, bind the handler's seams, and run it through the envelope."""
    command = PickCommand(order_id, key)

    async def handler(uow: UnitOfWork, cmd: PickCommand) -> PickResult:
        return await pick(uow, cmd, now=now, new_movement_id=new_movement_id)

    return await execute(uow_factory, command, handler, PickResult.decode)
