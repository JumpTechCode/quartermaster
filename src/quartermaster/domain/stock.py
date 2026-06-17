"""The stock quantity model: on-hand vs. reserved, and the operations on them.

``StockLevel`` is the in-memory mirror of a stock row's quantity columns
(design spec §3). It enforces the same CHECK constraints the database does —
``0 <= reserved <= on_hand`` — at construction, so an inconsistent level can
never be held. Its operations mirror the invariant-guarded conditional writes
(§5.2): each carries the guard the SQL ``WHERE`` clause would express, and an
operation that the guard forbids raises rather than producing a corrupt level.

``available = on_hand - reserved`` is the quantity free to be reserved.

This type is deliberately identity-free: ``sku_id`` and ``location_id`` belong
to the stock entity, not to the arithmetic. Levels are immutable; every
operation returns a new level, leaving the original untouched.
"""

from __future__ import annotations

from dataclasses import dataclass

from quartermaster.domain.errors import InsufficientStock, InvariantViolation


def _require_non_negative(qty: int) -> None:
    """A quantity is a programmer-supplied count; a negative one is a bug, not a
    business outcome, and must never slip past a ``>=`` guard."""
    if qty < 0:
        raise ValueError(f"quantity must be non-negative, got {qty}")


@dataclass(frozen=True)
class StockLevel:
    """On-hand and reserved quantities for one ``(sku, location)`` cell."""

    on_hand: int
    reserved: int

    def __post_init__(self) -> None:
        if self.on_hand < 0:
            raise InvariantViolation(f"on_hand must be >= 0, got {self.on_hand}")
        if self.reserved < 0:
            raise InvariantViolation(f"reserved must be >= 0, got {self.reserved}")
        if self.reserved > self.on_hand:
            raise InvariantViolation(
                f"reserved ({self.reserved}) must not exceed on_hand ({self.on_hand})"
            )

    @property
    def available(self) -> int:
        """Stock free to be reserved: ``on_hand - reserved`` (always >= 0)."""
        return self.on_hand - self.reserved

    def reserve(self, qty: int) -> StockLevel:
        """Reserve ``qty`` against available stock (allocate).

        Guard: ``available >= qty``. Raises :class:`InsufficientStock` otherwise.
        """
        _require_non_negative(qty)
        if qty > self.available:
            raise InsufficientStock(f"cannot reserve {qty}: only {self.available} available")
        return StockLevel(on_hand=self.on_hand, reserved=self.reserved + qty)

    def pick(self, qty: int) -> StockLevel:
        """Consume ``qty`` of reservation, removing it from the shelf (pick).

        Lowers both on-hand and reserved. Guard: ``reserved >= qty``. Raises
        :class:`InsufficientStock` otherwise.
        """
        _require_non_negative(qty)
        if qty > self.reserved:
            raise InsufficientStock(f"cannot pick {qty}: only {self.reserved} reserved")
        return StockLevel(on_hand=self.on_hand - qty, reserved=self.reserved - qty)

    def release(self, qty: int) -> StockLevel:
        """Release ``qty`` of held reservation back to available (cancel/expiry).

        Lowers reserved only. Guard: ``reserved >= qty``. Releasing more than is
        held would drive reserved negative — an invariant breach, not a shortage
        — so it raises :class:`InvariantViolation`.
        """
        _require_non_negative(qty)
        if qty > self.reserved:
            raise InvariantViolation(f"cannot release {qty}: only {self.reserved} reserved")
        return StockLevel(on_hand=self.on_hand, reserved=self.reserved - qty)

    def receive(self, qty: int) -> StockLevel:
        """Add ``qty`` to on-hand (receiving / putaway / restock)."""
        _require_non_negative(qty)
        return StockLevel(on_hand=self.on_hand + qty, reserved=self.reserved)
