"""The injected seam bundle the API closes over (assembled by the composition root)."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from quartermaster.application.clock import Clock
from quartermaster.application.ports import UnitOfWorkFactory
from quartermaster.domain.ids import MovementId, OrderId, ReceiptId, ReservationId


@dataclass(frozen=True)
class Deps:
    """Concrete seams, typed only against application/domain so api/ stays adapter-free.

    Commands run through ``uow_factory`` (READ COMMITTED, the guarded write path);
    multi-statement reads run through ``read_uow_factory`` (REPEATABLE READ) so a
    header and its lines come from one MVCC snapshot (issue #70).
    """

    uow_factory: UnitOfWorkFactory
    read_uow_factory: UnitOfWorkFactory
    now: Clock
    new_order_id: Callable[[], OrderId]
    new_receipt_id: Callable[[], ReceiptId]
    new_reservation_id: Callable[[], ReservationId]
    new_movement_id: Callable[[], MovementId]
