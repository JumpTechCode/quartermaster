"""Unit tests for command identity and fingerprinting."""

from __future__ import annotations

from uuid import UUID

from quartermaster.application.commands import (
    AllocateCommand,
    ArriveCommand,
    CreateOrderCommand,
    CreateReceiptCommand,
    ReceiveCommand,
)
from quartermaster.domain.ids import (
    IdempotencyKey,
    LocationId,
    OrderId,
    ReceiptId,
    SkuId,
)

ORDER_A = OrderId(UUID("00000000-0000-7000-8000-000000000001"))
ORDER_B = OrderId(UUID("00000000-0000-7000-8000-000000000002"))
_RID = ReceiptId(UUID("00000000-0000-7000-8000-000000000005"))


def test_fingerprint_is_stable_for_same_order() -> None:
    a = AllocateCommand(ORDER_A, IdempotencyKey("k1"))
    b = AllocateCommand(ORDER_A, IdempotencyKey("k2"))  # key differs, fingerprint must not
    assert a.fingerprint() == b.fingerprint()


def test_fingerprint_differs_by_order() -> None:
    a = AllocateCommand(ORDER_A, IdempotencyKey("k"))
    b = AllocateCommand(ORDER_B, IdempotencyKey("k"))
    assert a.fingerprint() != b.fingerprint()


def test_create_order_fingerprint_is_order_insensitive() -> None:
    a = CreateOrderCommand(((SkuId("A"), 1), (SkuId("B"), 2)), IdempotencyKey("k1"))
    b = CreateOrderCommand(((SkuId("B"), 2), (SkuId("A"), 1)), IdempotencyKey("k2"))
    assert a.fingerprint() == b.fingerprint()


def test_create_order_fingerprint_differs_on_qty() -> None:
    a = CreateOrderCommand(((SkuId("A"), 1),), IdempotencyKey("k"))
    b = CreateOrderCommand(((SkuId("A"), 2),), IdempotencyKey("k"))
    assert a.fingerprint() != b.fingerprint()


def test_pick_fingerprint_is_stable_across_keys() -> None:
    from quartermaster.application.commands import PickCommand

    a = PickCommand(ORDER_A, IdempotencyKey("k1"))
    b = PickCommand(ORDER_A, IdempotencyKey("k2"))
    assert a.fingerprint() == b.fingerprint()


def test_pick_fingerprint_differs_by_order() -> None:
    from quartermaster.application.commands import PickCommand

    a = PickCommand(ORDER_A, IdempotencyKey("k"))
    b = PickCommand(ORDER_B, IdempotencyKey("k"))
    assert a.fingerprint() != b.fingerprint()


def test_pick_fingerprint_differs_from_allocate() -> None:
    from quartermaster.application.commands import PickCommand

    assert (
        PickCommand(ORDER_A, IdempotencyKey("k")).fingerprint()
        != AllocateCommand(ORDER_A, IdempotencyKey("k")).fingerprint()
    )


def test_pack_fingerprint_is_stable_across_keys() -> None:
    from quartermaster.application.commands import PackCommand

    assert (
        PackCommand(ORDER_A, IdempotencyKey("k1")).fingerprint()
        == PackCommand(ORDER_A, IdempotencyKey("k2")).fingerprint()
    )


def test_pack_fingerprint_differs_from_pick() -> None:
    from quartermaster.application.commands import PackCommand, PickCommand

    assert (
        PackCommand(ORDER_A, IdempotencyKey("k")).fingerprint()
        != PickCommand(ORDER_A, IdempotencyKey("k")).fingerprint()
    )


def test_ship_fingerprint_differs_from_pack() -> None:
    from quartermaster.application.commands import PackCommand, ShipCommand

    assert (
        ShipCommand(ORDER_A, IdempotencyKey("k")).fingerprint()
        != PackCommand(ORDER_A, IdempotencyKey("k")).fingerprint()
    )


def test_cancel_fingerprint_differs_from_ship() -> None:
    from quartermaster.application.commands import CancelCommand, ShipCommand

    assert (
        CancelCommand(ORDER_A, IdempotencyKey("k")).fingerprint()
        != ShipCommand(ORDER_A, IdempotencyKey("k")).fingerprint()
    )


def test_create_receipt_fingerprint_is_line_order_independent() -> None:
    a = CreateReceiptCommand(((SkuId("A"), 1), (SkuId("B"), 2)), IdempotencyKey("k1"))
    b = CreateReceiptCommand(((SkuId("B"), 2), (SkuId("A"), 1)), IdempotencyKey("k2"))
    assert a.fingerprint() == b.fingerprint()


def test_arrive_fingerprint_independent_of_key() -> None:
    a = ArriveCommand(_RID, IdempotencyKey("k1"))
    b = ArriveCommand(_RID, IdempotencyKey("k2"))
    assert a.fingerprint() == b.fingerprint()


def test_receive_fingerprint_depends_on_location() -> None:
    a = ReceiveCommand(_RID, LocationId("L1"), ((SkuId("A"), 1),), IdempotencyKey("k"))
    b = ReceiveCommand(_RID, LocationId("L2"), ((SkuId("A"), 1),), IdempotencyKey("k"))
    assert a.fingerprint() != b.fingerprint()
