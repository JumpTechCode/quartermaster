"""The reservation-expiry reaper: a polled, idempotent, per-item pass.

Releases ``held`` reservations past their 15-minute ``expires_at`` (state →
``expired``, ``qty_reserved`` lowered, an ``EXPIRE`` movement appended), one
bounded transaction per reservation. It carries no idempotency key: the
reservation-state CAS (``held → expired``) is the exactly-once guard, so a
concurrent reaper or an explicit ``cancel`` racing the same row is a defined
no-op, exactly as in the ``cancel`` handler (design §5.4, §5.5).
"""

from __future__ import annotations

import logging
from collections.abc import Callable

from quartermaster.application.clock import Clock
from quartermaster.application.ports import UnitOfWorkFactory
from quartermaster.domain.errors import InvariantViolation
from quartermaster.domain.ids import IdempotencyKey, MovementId
from quartermaster.domain.movements import Movement, MovementType
from quartermaster.domain.state_machines import ReservationState
from quartermaster.workers.loop import ReaperRun

logger = logging.getLogger(__name__)


async def reap_reservations(
    uow_factory: UnitOfWorkFactory,
    *,
    now: Clock,
    new_movement_id: Callable[[], MovementId],
    batch_size: int,
) -> ReaperRun:
    """Expire up to ``batch_size`` due reservations, one transaction each."""
    async with uow_factory() as uow:
        due = await uow.reservations.due_for_expiry(now(), batch_size)

    acted = 0
    errors = 0
    for res in due:
        try:
            async with uow_factory() as uow:
                if not await uow.reservations.transition(
                    res.reservation_id, ReservationState.HELD, ReservationState.EXPIRED
                ):
                    continue  # another actor finalised it; defined no-op (design §4b)
                if not await uow.stock.release(res.sku_id, res.location_id, res.qty):
                    raise InvariantViolation(
                        f"reservation {res.reservation_id} was held but its stock is missing"
                    )
                await uow.movements.append(
                    Movement(
                        movement_id=new_movement_id(),
                        ts=now(),
                        type=MovementType.EXPIRE,
                        sku_id=res.sku_id,
                        from_location=res.location_id,
                        to_location=None,
                        qty=res.qty,
                        ref=res.order_id,
                        command_id=IdempotencyKey(f"reaper:expire:{res.reservation_id}"),
                    )
                )
                await uow.commit()
                acted += 1
        except Exception:
            logger.exception("reservation reaper failed on %s", res.reservation_id)
            errors += 1

    return ReaperRun(scanned=len(due), acted=acted, errors=errors)
