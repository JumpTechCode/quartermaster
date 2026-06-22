"""Typed domain errors.

The domain layer is pure: these exceptions describe business-rule and
state-machine violations without reference to any transport or storage concern.
Adapters and the API map them to the appropriate persistence or HTTP outcomes.
"""

from __future__ import annotations


class QuartermasterError(Exception):
    """Base class for all domain errors."""


class InvariantViolation(QuartermasterError):
    """A stock or ledger invariant would be violated by an operation.

    Reserved for a genuine consistency breach the system should never reach under
    correct operation -- e.g. a reservation an actor holds whose backing stock is
    gone. It is a server-side correctness alarm, not a client-reachable outcome;
    it rolls back and is surfaced as a classified 500, never cached as a business
    rejection. Contrast :class:`StockConflict`, the foreseeable client/concurrency
    shortfall on otherwise-valid input."""


class IllegalTransition(QuartermasterError):
    """A document state transition is not permitted from the current state."""


class InsufficientStock(QuartermasterError):
    """Not enough available stock to satisfy a reservation or a pick."""


class StockConflict(QuartermasterError):
    """A stock guard rejected an operation on otherwise-valid input: the named
    cell lacked enough unreserved stock to move (e.g. putaway from a location
    that does not currently hold the quantity, whether mis-addressed or raced by
    a concurrent mover). A foreseeable client/concurrency conflict mapped to 409,
    not the server-side :class:`InvariantViolation` breach."""


class IdempotencyKeyReuse(QuartermasterError):
    """An idempotency key was reused with a different command fingerprint."""


class OrderNotFound(QuartermasterError):
    """A command referenced an order that does not exist (a hard rejection)."""


class UnknownSku(QuartermasterError):
    """A create_order line referenced a SKU absent from the catalog (a hard rejection)."""


class ReceiptNotFound(QuartermasterError):
    """A command referenced a receipt that does not exist (a hard rejection)."""


class UnknownLocation(QuartermasterError):
    """A receive named a location absent from the catalog (a hard rejection)."""


class InvalidReceiptLine(QuartermasterError):
    """A receive line is absent from the receipt or would exceed its expected
    quantity (a hard rejection)."""


class LocationKindMismatch(QuartermasterError):
    """An inbound command named a location of the wrong kind: receiving into a
    shelf, or putting away to a non-shelf (a hard rejection). Allocation only
    reserves from shelves, so stock must stage at a non-shelf cell on receipt and
    only become pickable once put away to a shelf."""


class ReturnNotAllowed(QuartermasterError):
    """A return references an order not in a returnable (shipped) state, or a
    return line's SKU was not shipped on that order or exceeds the shipped
    quantity (a hard rejection)."""
