"""The ``ship`` command handler and its convenience runner.

Advances ``packed → shipped`` via a state/version CAS and finalizes
``shipped_qty = picked_qty`` on each line (guarded by ``add_shipped``). The pick
already removed the stock from the shelf, so ship touches no stock and appends no
movement; ``shipped_qty`` is the quantity the no-oversell oracle reads (design §2).
"""

from __future__ import annotations

from quartermaster.application.commands import ShipCommand
from quartermaster.application.envelope import execute
from quartermaster.application.errors import OccConflict
from quartermaster.application.ports import UnitOfWork, UnitOfWorkFactory
from quartermaster.application.results import ShippedLine, ShipResult
from quartermaster.domain.errors import OrderNotFound
from quartermaster.domain.ids import IdempotencyKey, OrderId
from quartermaster.domain.state_machines import ORDER_MACHINE, OrderState


async def ship(uow: UnitOfWork, command: ShipCommand) -> ShipResult:
    """Advance the order ``packed → shipped`` and finalize shipped quantities."""
    order = await uow.orders.get(command.order_id)
    if order is None:
        raise OrderNotFound(f"order {command.order_id} does not exist")
    ORDER_MACHINE.assert_legal(order.state, OrderState.SHIPPED)
    lines = await uow.orders.get_lines(command.order_id)
    if not await uow.orders.cas_state(
        command.order_id, order.state, order.version, OrderState.SHIPPED
    ):
        raise OccConflict(f"order {command.order_id} changed under ship")

    shipped: list[ShippedLine] = []
    for line in lines:
        to_ship = line.outstanding_to_ship
        if to_ship == 0:
            continue
        if not await uow.orders.add_shipped(command.order_id, line.sku_id, to_ship):
            raise OccConflict(f"order {command.order_id} line {line.sku_id} changed under ship")
        shipped.append(ShippedLine(line.sku_id, line.picked))

    return ShipResult(order_id=command.order_id, state=OrderState.SHIPPED, lines=tuple(shipped))


async def run_ship(
    uow_factory: UnitOfWorkFactory, order_id: OrderId, key: IdempotencyKey
) -> ShipResult:
    """Build the command and run it through the envelope."""
    command = ShipCommand(order_id, key)

    async def handler(uow: UnitOfWork, cmd: ShipCommand) -> ShipResult:
        return await ship(uow, cmd)

    return await execute(uow_factory, command, handler, ShipResult.decode)
