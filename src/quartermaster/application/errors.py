"""Application-layer control-flow signals for the transaction envelope.

These are not business-rule violations (those live in :mod:`quartermaster.domain`
errors); they steer the envelope's retry/abort behavior. They derive from
``QuartermasterError`` so every engine error shares one base.
"""

from __future__ import annotations

from quartermaster.domain.errors import QuartermasterError
from quartermaster.domain.ids import IdempotencyKey


class OccConflict(QuartermasterError):
    """A document compare-and-swap matched no row — retry the transaction."""


class RetryExhausted(QuartermasterError):
    """The bounded OCC retry budget was exhausted without success."""

    def __init__(self, key: IdempotencyKey) -> None:
        super().__init__(f"OCC retries exhausted for idempotency key {key!r}")
        self.key = key


class IdempotencyInFlight(QuartermasterError):
    """A duplicate request arrived while the original is still in flight.

    Only reachable if a durable ``pending`` row (or a ``succeeded`` row missing
    its response) is read back on replay -- impossible in the single-transaction
    envelope today, but defined as a typed "retry to fetch the result" outcome so
    the replay branch never depends on a strippable assert (issue #38).
    """

    def __init__(self, key: IdempotencyKey) -> None:
        super().__init__(f"idempotency key {key!r} is currently in flight; retry")
        self.key = key


class IdempotencyFinalizeError(QuartermasterError):
    """``finalize`` matched no pending row -- a double-finalize or a missing claim.

    A should-never-happen internal breach under the single-transaction envelope:
    the guarded ``UPDATE ... WHERE status = 'pending'`` updated zero rows, meaning
    the row was already terminal or absent (issue #38).
    """
