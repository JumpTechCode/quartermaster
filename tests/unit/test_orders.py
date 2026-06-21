"""OrderLine carries the partiality math; Order is a thin lifecycle document.

The line quantities advance monotonically (design spec §7 "state integrity"):
0 <= shipped <= picked <= allocated <= ordered, enforced at construction and by
guarded operations that mirror the SQL WHERE guards (§5.2). The property tests
are the spec's first testing pillar (§7).
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import pytest
from hypothesis import given
from hypothesis import strategies as st

from quartermaster.domain.errors import InvariantViolation
from quartermaster.domain.ids import OrderId, SkuId
from quartermaster.domain.orders import Order, OrderLine
from quartermaster.domain.quantities import MAX_QTY as COLUMN_MAX_QTY
from quartermaster.domain.state_machines import OrderState

MAX_QTY = 10_000
ORDER_ID = OrderId(uuid4())
SKU_ID = SkuId("WIDGET-1")


def line(ordered: int, allocated: int, picked: int, shipped: int) -> OrderLine:
    return OrderLine(
        order_id=ORDER_ID,
        sku_id=SKU_ID,
        ordered=ordered,
        allocated=allocated,
        picked=picked,
        shipped=shipped,
    )


@st.composite
def order_lines(draw: st.DrawFn) -> OrderLine:
    """A valid line: 0 <= shipped <= picked <= allocated <= ordered."""
    ordered = draw(st.integers(min_value=0, max_value=MAX_QTY))
    allocated = draw(st.integers(min_value=0, max_value=ordered))
    picked = draw(st.integers(min_value=0, max_value=allocated))
    shipped = draw(st.integers(min_value=0, max_value=picked))
    return line(ordered, allocated, picked, shipped)


# --- construction & invariants ---------------------------------------------


def test_valid_line_holds_its_quantities() -> None:
    ln = line(ordered=10, allocated=6, picked=4, shipped=2)
    assert (ln.ordered, ln.allocated, ln.picked, ln.shipped) == (10, 6, 4, 2)


@pytest.mark.parametrize(
    ("ordered", "allocated", "picked", "shipped"),
    [
        (10, 11, 0, 0),  # allocated > ordered
        (10, 5, 6, 0),  # picked > allocated
        (10, 5, 3, 4),  # shipped > picked
        (-1, 0, 0, 0),  # negative ordered
        (10, -1, 0, 0),  # negative allocated
    ],
)
def test_construction_rejects_non_monotonic(ordered, allocated, picked, shipped) -> None:  # type: ignore[no-untyped-def]
    with pytest.raises(InvariantViolation):
        line(ordered, allocated, picked, shipped)


def test_construction_rejects_quantity_above_column_max() -> None:
    with pytest.raises(InvariantViolation):
        line(ordered=COLUMN_MAX_QTY + 1, allocated=0, picked=0, shipped=0)


def test_construction_accepts_quantity_at_column_max() -> None:
    ln = line(ordered=COLUMN_MAX_QTY, allocated=COLUMN_MAX_QTY, picked=0, shipped=0)
    assert ln.ordered == COLUMN_MAX_QTY


def test_line_is_immutable() -> None:
    ln = line(10, 6, 4, 2)
    with pytest.raises(Exception):  # noqa: B017 - frozen dataclass raises FrozenInstanceError
        ln.allocated = 9  # type: ignore[misc]


# --- derived properties -----------------------------------------------------


def test_outstanding_quantities() -> None:
    ln = line(ordered=10, allocated=6, picked=4, shipped=1)
    assert ln.outstanding_to_allocate == 4
    assert ln.outstanding_to_pick == 2
    assert ln.outstanding_to_ship == 3


def test_is_fully_allocated() -> None:
    assert line(10, 10, 0, 0).is_fully_allocated is True
    assert line(10, 6, 0, 0).is_fully_allocated is False


# --- guarded operations -----------------------------------------------------


def test_allocate_advances_allocated() -> None:
    assert line(10, 4, 0, 0).allocate(3) == line(10, 7, 0, 0)


def test_allocate_beyond_ordered_raises() -> None:
    with pytest.raises(InvariantViolation):
        line(10, 8, 0, 0).allocate(3)


def test_pick_advances_picked() -> None:
    assert line(10, 6, 2, 0).pick(3) == line(10, 6, 5, 0)


def test_pick_beyond_allocated_raises() -> None:
    with pytest.raises(InvariantViolation):
        line(10, 6, 5, 0).pick(2)


def test_ship_advances_shipped() -> None:
    assert line(10, 6, 5, 1).ship(3) == line(10, 6, 5, 4)


def test_ship_beyond_picked_raises() -> None:
    with pytest.raises(InvariantViolation):
        line(10, 6, 5, 4).ship(2)


@pytest.mark.parametrize("op", ["allocate", "pick", "ship"])
def test_operations_reject_negative_quantities(op: str) -> None:
    ln = line(10, 6, 4, 2)
    with pytest.raises(ValueError):
        getattr(ln, op)(-1)


# --- property tests (spec §7, pillar 1) ------------------------------------


@given(ln=order_lines())
def test_monotonic_invariant_always_holds(ln: OrderLine) -> None:
    assert 0 <= ln.shipped <= ln.picked <= ln.allocated <= ln.ordered


@given(ln=order_lines(), data=st.data())
def test_allocate_then_advances_preserve_invariant(ln: OrderLine, data: st.DataObject) -> None:
    alloc = data.draw(st.integers(min_value=0, max_value=ln.outstanding_to_allocate))
    pick = data.draw(st.integers(min_value=0, max_value=ln.outstanding_to_pick))
    ship = data.draw(st.integers(min_value=0, max_value=ln.outstanding_to_ship))
    for result in (ln.allocate(alloc), ln.pick(pick), ln.ship(ship)):
        assert 0 <= result.shipped <= result.picked <= result.allocated <= result.ordered


# --- Order document ---------------------------------------------------------


def test_order_holds_its_fields() -> None:
    now = datetime.now(UTC)
    order = Order(order_id=ORDER_ID, state=OrderState.CREATED, version=1, created_at=now)
    assert order.state is OrderState.CREATED
    assert order.version == 1
    assert order.created_at == now


@pytest.mark.parametrize("version", [0, -1])
def test_order_rejects_version_below_one(version: int) -> None:
    with pytest.raises(InvariantViolation):
        Order(
            order_id=ORDER_ID,
            state=OrderState.CREATED,
            version=version,
            created_at=datetime.now(UTC),
        )
