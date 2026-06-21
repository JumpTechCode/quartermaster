"""The Receipt and Order lifecycles are table-driven and exhaustively legal.

These tests are the authoritative encoding of the transition tables from the
design spec (§4). The legal-transition sets below are written out longhand,
independent of the implementation, so the production tables are checked against
the spec rather than against a copy of themselves. Every ordered pair of states
is exercised: a pair is legal iff it appears in the longhand set, illegal
otherwise.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from itertools import product
from typing import Any

import pytest

from quartermaster.domain.errors import IllegalTransition
from quartermaster.domain.state_machines import (
    ORDER_MACHINE,
    RECEIPT_MACHINE,
    RESERVATION_MACHINE,
    OrderState,
    ReceiptState,
    ReservationState,
    StateMachine,
)

# --- The spec, written out longhand (§4) -----------------------------------

LEGAL_RECEIPT_TRANSITIONS: set[tuple[ReceiptState, ReceiptState]] = {
    (ReceiptState.EXPECTED, ReceiptState.ARRIVED),
    (ReceiptState.EXPECTED, ReceiptState.CANCELLED),
    (ReceiptState.ARRIVED, ReceiptState.RECEIVING),
    (ReceiptState.ARRIVED, ReceiptState.CANCELLED),
    (ReceiptState.RECEIVING, ReceiptState.RECEIVED),
    (ReceiptState.RECEIVED, ReceiptState.PUTAWAY_COMPLETE),
    (ReceiptState.PUTAWAY_COMPLETE, ReceiptState.CLOSED),
}
TERMINAL_RECEIPT_STATES: set[ReceiptState] = {ReceiptState.CLOSED, ReceiptState.CANCELLED}

LEGAL_ORDER_TRANSITIONS: set[tuple[OrderState, OrderState]] = {
    (OrderState.CREATED, OrderState.ALLOCATED),
    (OrderState.CREATED, OrderState.BACKORDERED),
    (OrderState.CREATED, OrderState.CANCELLED),
    (OrderState.ALLOCATED, OrderState.PICKING),
    (OrderState.ALLOCATED, OrderState.CANCELLED),
    (OrderState.ALLOCATED, OrderState.BACKORDERED),
    (OrderState.BACKORDERED, OrderState.ALLOCATED),
    (OrderState.BACKORDERED, OrderState.CANCELLED),
    # cancel is release-only and stops here: created/allocated/backordered are
    # the states that still hold (or have yet to take) reservations, so cancel
    # is a pure -reserved release. Once a pick starts, stock has physically left
    # the shelf; unwinding it is a restock that flows through the Receipt/RMA
    # path, never an inline state flip. See ADR / spec §4 ("pre-pick").
    (OrderState.PICKING, OrderState.PICKED),
    (OrderState.PICKED, OrderState.PACKED),
    (OrderState.PACKED, OrderState.SHIPPED),
    (OrderState.SHIPPED, OrderState.CLOSED),
}
TERMINAL_ORDER_STATES: set[OrderState] = {OrderState.CLOSED, OrderState.CANCELLED}

LEGAL_RESERVATION_TRANSITIONS: set[tuple[ReservationState, ReservationState]] = {
    (ReservationState.HELD, ReservationState.CONSUMED),
    (ReservationState.HELD, ReservationState.RELEASED),
    (ReservationState.HELD, ReservationState.EXPIRED),
    # consumed (pick) / released (cancel) / expired (TTL reaper) are terminal.
    # released and expired both lower qty_reserved but stay distinct for audit.
}
TERMINAL_RESERVATION_STATES: set[ReservationState] = {
    ReservationState.CONSUMED,
    ReservationState.RELEASED,
    ReservationState.EXPIRED,
}


@dataclass(frozen=True)
class Case:
    """A machine paired with the spec it must satisfy."""

    name: str
    machine: StateMachine[Any]
    states: type[Enum]
    legal: set[tuple[Any, Any]]
    terminal: set[Any]


CASES: list[Case] = [
    Case(
        "receipt", RECEIPT_MACHINE, ReceiptState, LEGAL_RECEIPT_TRANSITIONS, TERMINAL_RECEIPT_STATES
    ),
    Case("order", ORDER_MACHINE, OrderState, LEGAL_ORDER_TRANSITIONS, TERMINAL_ORDER_STATES),
    Case(
        "reservation",
        RESERVATION_MACHINE,
        ReservationState,
        LEGAL_RESERVATION_TRANSITIONS,
        TERMINAL_RESERVATION_STATES,
    ),
]
CASE_IDS = [c.name for c in CASES]


@pytest.mark.parametrize("case", CASES, ids=CASE_IDS)
def test_table_defines_every_state(case: Case) -> None:
    """Every state in the enum is a key in the transition table (no gaps)."""
    for state in case.states:
        assert state in case.machine.transitions


@pytest.mark.parametrize("case", CASES, ids=CASE_IDS)
def test_table_is_closed_over_its_states(case: Case) -> None:
    """Every reachable successor is itself a defined state."""
    for successors in case.machine.transitions.values():
        for successor in successors:
            assert successor in case.machine.transitions


@pytest.mark.parametrize("case", CASES, ids=CASE_IDS)
def test_every_ordered_pair_matches_the_spec(case: Case) -> None:
    """A transition is legal iff the spec lists it; all other pairs are rejected."""
    for current, proposed in product(case.states, case.states):
        expected = (current, proposed) in case.legal
        assert case.machine.is_legal(current, proposed) is expected, (
            f"{case.name}: {current} -> {proposed} should be {'legal' if expected else 'illegal'}"
        )


@pytest.mark.parametrize("case", CASES, ids=CASE_IDS)
def test_legal_next_states_matches_the_spec(case: Case) -> None:
    for state in case.states:
        expected = {to for (frm, to) in case.legal if frm == state}
        assert case.machine.legal_next_states(state) == frozenset(expected)


@pytest.mark.parametrize("case", CASES, ids=CASE_IDS)
def test_terminal_states_have_no_successors(case: Case) -> None:
    for state in case.states:
        assert case.machine.is_terminal(state) is (state in case.terminal)
        if state in case.terminal:
            assert case.machine.legal_next_states(state) == frozenset()


@pytest.mark.parametrize("case", CASES, ids=CASE_IDS)
def test_assert_legal_passes_on_every_legal_transition(case: Case) -> None:
    for current, proposed in case.legal:
        case.machine.assert_legal(current, proposed)  # must not raise


@pytest.mark.parametrize("case", CASES, ids=CASE_IDS)
def test_assert_legal_raises_on_illegal_transition(case: Case) -> None:
    for current, proposed in product(case.states, case.states):
        if (current, proposed) in case.legal:
            continue
        with pytest.raises(IllegalTransition) as excinfo:
            case.machine.assert_legal(current, proposed)
        message = str(excinfo.value)
        assert current.value in message
        assert proposed.value in message
        assert case.machine.name in message


def test_machines_are_named_for_diagnostics() -> None:
    assert RECEIPT_MACHINE.name == "receipt"
    assert ORDER_MACHINE.name == "order"
    assert RESERVATION_MACHINE.name == "reservation"


def test_state_is_usable_as_its_database_string() -> None:
    """A StrEnum member *is* its text, so it binds straight into the SQL guard."""
    assert ReceiptState.PUTAWAY_COMPLETE.value == "putaway_complete"
    assert OrderState.BACKORDERED.value == "backordered"
    # Subclass of str: assignable where a plain str (a SQL parameter) is expected.
    db_value: str = OrderState.ALLOCATED
    assert db_value == "allocated"
