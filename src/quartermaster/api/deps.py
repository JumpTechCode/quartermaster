"""The injected seam bundle the API closes over (assembled by the composition root)."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from quartermaster.application.clock import Clock
from quartermaster.application.ports import UnitOfWorkFactory
from quartermaster.domain.ids import MovementId, OrderId, ReservationId


@dataclass(frozen=True)
class Deps:
    """Concrete seams, typed only against application/domain so api/ stays adapter-free."""

    uow_factory: UnitOfWorkFactory
    now: Clock
    new_order_id: Callable[[], OrderId]
    new_reservation_id: Callable[[], ReservationId]
    new_movement_id: Callable[[], MovementId]
