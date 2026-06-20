"""The ``pack`` command handler and its convenience runner.

A pure document transition: ``picked → packed`` via a state/version CAS. The pick
already removed stock from the shelf, so pack touches no stock and appends no
movement — it is a lifecycle gate (design §2).
"""

from __future__ import annotations

from quartermaster.application.commands import PackCommand
from quartermaster.application.envelope import execute
from quartermaster.application.errors import OccConflict
from quartermaster.application.ports import UnitOfWork, UnitOfWorkFactory
from quartermaster.application.results import PackResult
from quartermaster.domain.errors import OrderNotFound
from quartermaster.domain.ids import IdempotencyKey, OrderId
from quartermaster.domain.state_machines import ORDER_MACHINE, OrderState


async def pack(uow: UnitOfWork, command: PackCommand) -> PackResult:
    """Advance the order ``picked → packed``."""
    order = await uow.orders.get(command.order_id)
    if order is None:
        raise OrderNotFound(f"order {command.order_id} does not exist")
    ORDER_MACHINE.assert_legal(order.state, OrderState.PACKED)
    if not await uow.orders.cas_state(
        command.order_id, order.state, order.version, OrderState.PACKED
    ):
        raise OccConflict(f"order {command.order_id} changed under pack")
    return PackResult(order_id=command.order_id, state=OrderState.PACKED)


async def run_pack(
    uow_factory: UnitOfWorkFactory, order_id: OrderId, key: IdempotencyKey
) -> PackResult:
    """Build the command and run it through the envelope."""
    command = PackCommand(order_id, key)

    async def handler(uow: UnitOfWork, cmd: PackCommand) -> PackResult:
        return await pack(uow, cmd)

    return await execute(uow_factory, command, handler, PackResult.decode)
