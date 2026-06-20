"""The domain error hierarchy is coherent: every error is a QuartermasterError."""

from __future__ import annotations

import pytest

from quartermaster.domain.errors import (
    IdempotencyKeyReuse,
    IllegalTransition,
    InsufficientStock,
    InvalidReceiptLine,
    InvariantViolation,
    OrderNotFound,
    QuartermasterError,
    ReceiptNotFound,
    UnknownLocation,
)

DOMAIN_ERRORS: list[type[QuartermasterError]] = [
    InvariantViolation,
    IllegalTransition,
    InsufficientStock,
    IdempotencyKeyReuse,
    OrderNotFound,
]


@pytest.mark.parametrize("error_type", DOMAIN_ERRORS)
def test_domain_errors_subclass_base(error_type: type[QuartermasterError]) -> None:
    assert issubclass(error_type, QuartermasterError)


@pytest.mark.parametrize("error_type", DOMAIN_ERRORS)
def test_domain_errors_are_catchable_as_base(error_type: type[QuartermasterError]) -> None:
    with pytest.raises(QuartermasterError):
        raise error_type("boom")


def test_order_not_found_is_a_quartermaster_error() -> None:
    assert issubclass(OrderNotFound, QuartermasterError)


def test_inbound_errors_are_quartermaster_errors() -> None:
    for exc_type in (ReceiptNotFound, UnknownLocation, InvalidReceiptLine):
        err = exc_type("boom")
        assert isinstance(err, QuartermasterError)
        assert str(err) == "boom"
