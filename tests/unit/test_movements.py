"""Movement is the append-only ledger record (design spec §3, §7). The offline
conservation oracle sums only the on-hand-affecting types (RECEIVE +, PICK —,
PUTAWAY net-zero); the record itself carries the faithful shape and the one
structural invariant that always holds: a movement moves a positive quantity.
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import pytest

from quartermaster.domain.errors import InvariantViolation
from quartermaster.domain.ids import (
    IdempotencyKey,
    LocationId,
    MovementId,
    OrderId,
    SkuId,
)
from quartermaster.domain.movements import Movement, MovementType


def movement(qty: int, type: MovementType = MovementType.RECEIVE) -> Movement:
    return Movement(
        movement_id=MovementId(uuid4()),
        ts=datetime.now(UTC),
        type=type,
        sku_id=SkuId("WIDGET-1"),
        from_location=None,
        to_location=LocationId("RECV"),
        qty=qty,
        ref=OrderId(uuid4()),
        command_id=IdempotencyKey("idem-123"),
    )


def test_movement_holds_its_fields() -> None:
    mv = movement(qty=5, type=MovementType.PUTAWAY)
    assert mv.qty == 5
    assert mv.type is MovementType.PUTAWAY
    assert mv.command_id == "idem-123"


def test_movement_types_match_the_spec() -> None:
    assert {t.value for t in MovementType} == {
        "receive",
        "putaway",
        "pick",
        "reserve",
        "release",
        "expire",
    }


@pytest.mark.parametrize("qty", [0, -1])
def test_movement_rejects_non_positive_qty(qty: int) -> None:
    with pytest.raises(InvariantViolation):
        movement(qty)


def test_movement_is_immutable() -> None:
    mv = movement(qty=5)
    with pytest.raises(Exception):  # noqa: B017 - frozen dataclass raises FrozenInstanceError
        mv.qty = 9  # type: ignore[misc]


def test_expire_movement_type_exists() -> None:
    from quartermaster.domain.movements import MovementType

    assert MovementType.EXPIRE.value == "expire"
