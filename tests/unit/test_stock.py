"""StockLevel is the in-memory mirror of a stock row's quantity columns.

It holds ``(qty_on_hand, qty_reserved)`` and enforces the same CHECK constraints
the database does (design spec §3): ``0 <= reserved <= on_hand``. Its operations
mirror the invariant-guarded conditional writes (§5.2): a guard that the SQL
``WHERE`` would express must hold, or the operation raises rather than producing
an inconsistent level. Identity (``sku_id``/``location_id``) lives on the entity,
not here — this type is purely the quantity arithmetic and its invariants.

The property-based tests are the spec's first testing pillar (§7): random valid
levels and quantities, asserting the invariants hold after every operation.
"""

from __future__ import annotations

import pytest
from hypothesis import given
from hypothesis import strategies as st

from quartermaster.domain.errors import InsufficientStock, InvariantViolation
from quartermaster.domain.stock import StockLevel

MAX_QTY = 10_000


@st.composite
def stock_levels(draw: st.DrawFn) -> StockLevel:
    """A valid level: 0 <= reserved <= on_hand."""
    on_hand = draw(st.integers(min_value=0, max_value=MAX_QTY))
    reserved = draw(st.integers(min_value=0, max_value=on_hand))
    return StockLevel(on_hand=on_hand, reserved=reserved)


# --- Construction & invariants ---------------------------------------------


def test_available_is_on_hand_minus_reserved() -> None:
    assert StockLevel(on_hand=10, reserved=3).available == 7


def test_empty_level_is_consistent() -> None:
    level = StockLevel(on_hand=0, reserved=0)
    assert level.available == 0


@pytest.mark.parametrize(
    ("on_hand", "reserved"),
    [(-1, 0), (0, -1), (5, 6), (-3, -3)],
)
def test_construction_rejects_inconsistent_levels(on_hand: int, reserved: int) -> None:
    with pytest.raises(InvariantViolation):
        StockLevel(on_hand=on_hand, reserved=reserved)


def test_level_is_immutable() -> None:
    level = StockLevel(on_hand=5, reserved=2)
    with pytest.raises(Exception):  # noqa: B017 - frozen dataclass raises FrozenInstanceError
        level.on_hand = 9  # type: ignore[misc]


# --- reserve ----------------------------------------------------------------


def test_reserve_raises_reserved_and_lowers_available() -> None:
    level = StockLevel(on_hand=10, reserved=2).reserve(3)
    assert level == StockLevel(on_hand=10, reserved=5)
    assert level.available == 5


def test_reserve_beyond_available_raises_insufficient_stock() -> None:
    level = StockLevel(on_hand=10, reserved=8)  # available == 2
    with pytest.raises(InsufficientStock):
        level.reserve(3)


def test_reserve_exactly_available_succeeds() -> None:
    level = StockLevel(on_hand=10, reserved=8).reserve(2)
    assert level.reserved == 10
    assert level.available == 0


# --- pick (consume reservation) --------------------------------------------


def test_pick_lowers_both_on_hand_and_reserved() -> None:
    level = StockLevel(on_hand=10, reserved=6).pick(4)
    assert level == StockLevel(on_hand=6, reserved=2)


def test_pick_beyond_reserved_raises_insufficient_stock() -> None:
    level = StockLevel(on_hand=10, reserved=3)
    with pytest.raises(InsufficientStock):
        level.pick(4)


# --- release (cancel / expiry) ---------------------------------------------


def test_release_lowers_reserved_only() -> None:
    level = StockLevel(on_hand=10, reserved=6).release(4)
    assert level == StockLevel(on_hand=10, reserved=2)
    assert level.available == 8


def test_release_beyond_reserved_raises_invariant_violation() -> None:
    level = StockLevel(on_hand=10, reserved=3)
    with pytest.raises(InvariantViolation):
        level.release(4)


# --- receive / restock ------------------------------------------------------


def test_receive_raises_on_hand_only() -> None:
    level = StockLevel(on_hand=10, reserved=6).receive(5)
    assert level == StockLevel(on_hand=15, reserved=6)
    assert level.available == 9


# --- negative quantities are programmer errors ------------------------------


@pytest.mark.parametrize("op", ["reserve", "pick", "release", "receive"])
def test_operations_reject_negative_quantities(op: str) -> None:
    level = StockLevel(on_hand=10, reserved=5)
    with pytest.raises(ValueError):
        getattr(level, op)(-1)


# --- properties (spec §7, pillar 1) ----------------------------------------


@given(level=stock_levels())
def test_available_is_always_in_range(level: StockLevel) -> None:
    assert 0 <= level.available <= level.on_hand


@given(level=stock_levels(), data=st.data())
def test_reserve_then_release_round_trips(level: StockLevel, data: st.DataObject) -> None:
    qty = data.draw(st.integers(min_value=0, max_value=level.available))
    assert level.reserve(qty).release(qty) == level


@given(level=stock_levels(), data=st.data())
def test_pick_preserves_available(level: StockLevel, data: st.DataObject) -> None:
    qty = data.draw(st.integers(min_value=0, max_value=level.reserved))
    picked = level.pick(qty)
    # picking lowers on_hand and reserved equally, so available is unchanged
    assert picked.available == level.available
    assert picked.on_hand == level.on_hand - qty
    assert picked.reserved == level.reserved - qty


@given(level=stock_levels(), data=st.data())
def test_every_legal_operation_preserves_the_invariant(
    level: StockLevel, data: st.DataObject
) -> None:
    reserve_qty = data.draw(st.integers(min_value=0, max_value=level.available))
    consume_qty = data.draw(st.integers(min_value=0, max_value=level.reserved))
    receive_qty = data.draw(st.integers(min_value=0, max_value=MAX_QTY))
    for result in (
        level.reserve(reserve_qty),
        level.pick(consume_qty),
        level.release(consume_qty),
        level.receive(receive_qty),
    ):
        assert 0 <= result.reserved <= result.on_hand


@given(level=stock_levels(), data=st.data())
def test_reserve_beyond_available_always_rejected(level: StockLevel, data: st.DataObject) -> None:
    qty = data.draw(st.integers(min_value=level.available + 1, max_value=level.available + MAX_QTY))
    with pytest.raises(InsufficientStock):
        level.reserve(qty)
