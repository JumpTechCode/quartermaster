"""The ``allocate`` command handler and its convenience runner.

Reserves each order line greedily across the SKU's locations (location-ordered),
recording a held reservation and a RESERVE movement per partial fill, then CASes
the order header to ``allocated`` (all lines full) or ``backordered`` (any
shortfall). A re-allocation of a backordered order that gains some stock but
stays short is a legal no-state-change version bump; one that gains nothing skips
the CAS entirely (no write, no version bump) so a perpetually unfillable order is
not rewritten every sweep tick (issue #67). A 0-row CAS is an ``OccConflict`` the
envelope retries. The handler is pure orchestration over the ports; time and id
generation enter via injected callables.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import timedelta

from quartermaster.application.clock import Clock
from quartermaster.application.commands import AllocateCommand
from quartermaster.application.envelope import execute
from quartermaster.application.errors import OccConflict
from quartermaster.application.ports import UnitOfWork, UnitOfWorkFactory
from quartermaster.application.results import AllocateResult, LineAllocation
from quartermaster.domain.errors import IllegalTransition, OrderNotFound
from quartermaster.domain.ids import IdempotencyKey, MovementId, OrderId, ReservationId
from quartermaster.domain.movements import Movement, MovementType
from quartermaster.domain.reservations import Reservation
from quartermaster.domain.state_machines import ORDER_MACHINE, OrderState, ReservationState

RESERVATION_TTL = timedelta(minutes=15)
_ALLOCATE_SOURCES = frozenset({OrderState.CREATED, OrderState.BACKORDERED})


async def allocate(
    uow: UnitOfWork,
    command: AllocateCommand,
    *,
    now: Clock,
    new_reservation_id: Callable[[], ReservationId],
    new_movement_id: Callable[[], MovementId],
) -> AllocateResult:
    """Reserve available stock for every outstanding line of the order."""
    order = await uow.orders.get(command.order_id)
    if order is None:
        raise OrderNotFound(f"order {command.order_id} does not exist")
    if order.state not in _ALLOCATE_SOURCES:
        raise IllegalTransition(f"order: cannot allocate from {order.state.value}")

    lines = await uow.orders.get_lines(command.order_id)
    line_allocations: list[LineAllocation] = []
    reservation_ids: list[ReservationId] = []
    fully_allocated = True
    any_allocated = False

    for line in lines:
        remaining = line.outstanding_to_allocate
        for location, _available in await uow.stock.stock_locations(line.sku_id):
            if remaining == 0:
                break
            take = await uow.stock.reserve_up_to(line.sku_id, location, remaining)
            if take == 0:
                continue
            reservation_id = new_reservation_id()
            await uow.reservations.add(
                Reservation(
                    reservation_id=reservation_id,
                    order_id=command.order_id,
                    sku_id=line.sku_id,
                    location_id=location,
                    qty=take,
                    state=ReservationState.HELD,
                    expires_at=now() + RESERVATION_TTL,
                )
            )
            await uow.movements.append(
                Movement(
                    movement_id=new_movement_id(),
                    ts=now(),
                    type=MovementType.RESERVE,
                    sku_id=line.sku_id,
                    from_location=None,
                    to_location=location,
                    qty=take,
                    ref=command.order_id,
                    command_id=command.key,
                )
            )
            if not await uow.orders.add_allocated(command.order_id, line.sku_id, take):
                raise OccConflict(
                    f"order {command.order_id} line {line.sku_id} changed under allocate"
                )
            reservation_ids.append(reservation_id)
            remaining -= take
        allocated_this_line = line.outstanding_to_allocate - remaining
        if allocated_this_line > 0:
            any_allocated = True
        line_allocations.append(LineAllocation(line.sku_id, allocated_this_line))
        if remaining > 0:
            fully_allocated = False

    target = OrderState.ALLOCATED if fully_allocated else OrderState.BACKORDERED
    state_changes = target != order.state
    if state_changes:
        ORDER_MACHINE.assert_legal(order.state, target)
    # Skip the header CAS entirely when nothing changed: a re-swept order that
    # gained no allocation and remains backordered must not burn a row write,
    # dead tuple, WAL record, and version bump on every sweep tick (issue #67).
    # A real state change or any new line allocation still CASes -- both to
    # persist the transition and to keep the version bump that serializes
    # concurrent allocate/cancel against the lines this pass just changed.
    if (state_changes or any_allocated) and not await uow.orders.cas_state(
        command.order_id, order.state, order.version, target
    ):
        raise OccConflict(f"order {command.order_id} changed under allocate")

    return AllocateResult(
        order_id=command.order_id,
        state=target,
        lines=tuple(line_allocations),
        reservation_ids=tuple(reservation_ids),
    )


async def run_allocate(
    uow_factory: UnitOfWorkFactory,
    order_id: OrderId,
    key: IdempotencyKey,
    *,
    now: Clock,
    new_reservation_id: Callable[[], ReservationId],
    new_movement_id: Callable[[], MovementId],
) -> AllocateResult:
    """Build the command, bind the handler's seams, and run it through the envelope."""
    command = AllocateCommand(order_id, key)

    async def handler(uow: UnitOfWork, cmd: AllocateCommand) -> AllocateResult:
        return await allocate(
            uow,
            cmd,
            now=now,
            new_reservation_id=new_reservation_id,
            new_movement_id=new_movement_id,
        )

    return await execute(uow_factory, command, handler, AllocateResult.decode)
