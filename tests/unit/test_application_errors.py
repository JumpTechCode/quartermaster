"""Unit tests for the envelope's control-flow signals."""

from __future__ import annotations

from quartermaster.application.errors import OccConflict, RetryExhausted
from quartermaster.domain.errors import QuartermasterError
from quartermaster.domain.ids import IdempotencyKey


def test_signals_are_quartermaster_errors() -> None:
    assert issubclass(OccConflict, QuartermasterError)
    assert issubclass(RetryExhausted, QuartermasterError)


def test_retry_exhausted_carries_the_key() -> None:
    err = RetryExhausted(IdempotencyKey("abc"))
    assert err.key == "abc"
