"""Unit tests for command identity and fingerprinting."""

from __future__ import annotations

from uuid import UUID

from quartermaster.application.commands import AllocateCommand
from quartermaster.domain.ids import IdempotencyKey, OrderId

ORDER_A = OrderId(UUID("00000000-0000-7000-8000-000000000001"))
ORDER_B = OrderId(UUID("00000000-0000-7000-8000-000000000002"))


def test_fingerprint_is_stable_for_same_order() -> None:
    a = AllocateCommand(ORDER_A, IdempotencyKey("k1"))
    b = AllocateCommand(ORDER_A, IdempotencyKey("k2"))  # key differs, fingerprint must not
    assert a.fingerprint() == b.fingerprint()


def test_fingerprint_differs_by_order() -> None:
    a = AllocateCommand(ORDER_A, IdempotencyKey("k"))
    b = AllocateCommand(ORDER_B, IdempotencyKey("k"))
    assert a.fingerprint() != b.fingerprint()
