"""Typed domain errors.

The domain layer is pure: these exceptions describe business-rule and
state-machine violations without reference to any transport or storage concern.
Adapters and the API map them to the appropriate persistence or HTTP outcomes.
"""

from __future__ import annotations


class QuartermasterError(Exception):
    """Base class for all domain errors."""


class InvariantViolation(QuartermasterError):
    """A stock or ledger invariant would be violated by an operation."""


class IllegalTransition(QuartermasterError):
    """A document state transition is not permitted from the current state."""


class InsufficientStock(QuartermasterError):
    """Not enough available stock to satisfy a reservation or a pick."""


class IdempotencyKeyReuse(QuartermasterError):
    """An idempotency key was reused with a different command fingerprint."""


class OrderNotFound(QuartermasterError):
    """A command referenced an order that does not exist (a hard rejection)."""


class UnknownSku(QuartermasterError):
    """A create_order line referenced a SKU absent from the catalog (a hard rejection)."""
