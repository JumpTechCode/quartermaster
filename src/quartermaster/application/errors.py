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
