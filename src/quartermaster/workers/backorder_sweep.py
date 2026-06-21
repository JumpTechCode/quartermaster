"""The backorder fulfilment sweep: a polled worker that re-allocates backordered
orders FIFO by age, one bounded transaction per order.

It re-runs the standard isolated ``allocate`` (which already accepts a
``backordered`` source and reserves only each line's outstanding quantity), so
the sweep adds no allocation logic of its own. Like the reapers it bypasses the
idempotency envelope: the order-state CAS and the invariant-guarded conditional
reserve are the guards (design §4, §5.5; ADRs 0007, 0016, 0017, 0018). This is
what decouples inbound from outbound — ``putaway`` never re-allocates inline.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass

from quartermaster.application.clock import Clock
from quartermaster.application.commands import AllocateCommand
from quartermaster.application.handlers.allocate import allocate
from quartermaster.application.ports import UnitOfWorkFactory
from quartermaster.domain.ids import IdempotencyKey, MovementId, ReservationId
from quartermaster.domain.state_machines import OrderState

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class SweepRun:
    """Telemetry for one bounded backorder-sweep pass."""

    scanned: int = 0
    allocated: int = 0
    still_backordered: int = 0
    errors: int = 0


async def sweep_backorders(
    uow_factory: UnitOfWorkFactory,
    *,
    now: Clock,
    new_reservation_id: Callable[[], ReservationId],
    new_movement_id: Callable[[], MovementId],
    batch_size: int,
) -> SweepRun:
    """Re-allocate up to ``batch_size`` backordered orders, one transaction each."""
    async with uow_factory() as uow:
        order_ids = await uow.orders.backordered_orders(batch_size)

    allocated = 0
    still_backordered = 0
    errors = 0
    for order_id in order_ids:
        try:
            async with uow_factory() as uow:
                result = await allocate(
                    uow,
                    AllocateCommand(order_id, IdempotencyKey(f"sweep:{order_id}")),
                    now=now,
                    new_reservation_id=new_reservation_id,
                    new_movement_id=new_movement_id,
                )
                await uow.commit()
            if result.state is OrderState.ALLOCATED:
                allocated += 1
            else:
                still_backordered += 1
        except Exception:
            logger.exception("backorder sweep failed on %s", order_id)
            errors += 1

    return SweepRun(
        scanned=len(order_ids),
        allocated=allocated,
        still_backordered=still_backordered,
        errors=errors,
    )
