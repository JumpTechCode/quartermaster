"""Document lifecycle state machines (the inbound Receipt and outbound Order).

Transitions *are* the commands. This module holds the **allowed-transition
tables** in the pure domain layer, table-driven and exhaustively unit-tested
(design spec §4). At runtime the Postgres guarded ``UPDATE`` enforces the same
tables atomically via a state/version compare-and-swap; this module is the
single source of truth those guards mirror.

The tables encode *legality* — which ``(current -> proposed)`` transitions are
permitted — not the side effects. Whether an ``allocate`` lands an order in
``allocated`` or ``backordered``, or what a ``cancel`` must release, is an
application-layer decision driven by stock availability; both target states are
legal from ``created`` here, and the handler chooses between them.

States are :class:`enum.StrEnum` so a member is its own wire/database string:
``OrderState.ALLOCATED == "allocated"``, which is exactly the text stored in the
document ``state`` column and compared in the CAS ``WHERE``.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import Enum, StrEnum

from quartermaster.domain.errors import IllegalTransition


class ReceiptState(StrEnum):
    """Inbound document lifecycle (supplier receipts and customer RMAs alike)."""

    EXPECTED = "expected"
    ARRIVED = "arrived"
    RECEIVING = "receiving"
    RECEIVED = "received"
    PUTAWAY_COMPLETE = "putaway_complete"
    CLOSED = "closed"
    CANCELLED = "cancelled"


class OrderState(StrEnum):
    """Outbound document lifecycle."""

    CREATED = "created"
    ALLOCATED = "allocated"
    BACKORDERED = "backordered"
    PICKING = "picking"
    PICKED = "picked"
    PACKED = "packed"
    SHIPPED = "shipped"
    CLOSED = "closed"
    CANCELLED = "cancelled"


class ReservationState(StrEnum):
    """Reservation lifecycle (design spec §3, §5.4)."""

    HELD = "held"
    CONSUMED = "consumed"
    RELEASED = "released"
    EXPIRED = "expired"


@dataclass(frozen=True)
class StateMachine[S: Enum]:
    """A table-driven guard over a document's legal state transitions.

    ``transitions`` maps each state to the frozen set of states reachable from
    it in one step; a state with an empty set is terminal. The machine answers
    legality questions and raises :class:`IllegalTransition` on a forbidden move.
    """

    name: str
    transitions: Mapping[S, frozenset[S]]

    def legal_next_states(self, current: S) -> frozenset[S]:
        """The states reachable from ``current`` in one legal transition."""
        return self.transitions.get(current, frozenset())

    def is_legal(self, current: S, proposed: S) -> bool:
        """Whether ``current -> proposed`` is a permitted transition."""
        return proposed in self.legal_next_states(current)

    def is_terminal(self, state: S) -> bool:
        """Whether ``state`` has no outgoing transitions."""
        return not self.legal_next_states(state)

    def assert_legal(self, current: S, proposed: S) -> None:
        """Raise :class:`IllegalTransition` unless ``current -> proposed`` is legal."""
        if not self.is_legal(current, proposed):
            raise IllegalTransition(
                f"{self.name}: illegal transition {current.value} -> {proposed.value}"
            )


RECEIPT_MACHINE: StateMachine[ReceiptState] = StateMachine(
    name="receipt",
    transitions={
        ReceiptState.EXPECTED: frozenset({ReceiptState.ARRIVED, ReceiptState.CANCELLED}),
        ReceiptState.ARRIVED: frozenset({ReceiptState.RECEIVING, ReceiptState.CANCELLED}),
        ReceiptState.RECEIVING: frozenset({ReceiptState.RECEIVED}),
        ReceiptState.RECEIVED: frozenset({ReceiptState.PUTAWAY_COMPLETE}),
        ReceiptState.PUTAWAY_COMPLETE: frozenset({ReceiptState.CLOSED}),
        ReceiptState.CLOSED: frozenset(),
        ReceiptState.CANCELLED: frozenset(),
    },
)

ORDER_MACHINE: StateMachine[OrderState] = StateMachine(
    name="order",
    transitions={
        OrderState.CREATED: frozenset(
            {OrderState.ALLOCATED, OrderState.BACKORDERED, OrderState.CANCELLED}
        ),
        OrderState.ALLOCATED: frozenset({OrderState.PICKING, OrderState.CANCELLED}),
        OrderState.BACKORDERED: frozenset({OrderState.ALLOCATED, OrderState.CANCELLED}),
        # cancel is release-only: legal only while reservations are still held
        # (created/allocated/backordered). From picking onward stock has left the
        # shelf, so the unwind is a physical restock via the Receipt/RMA path —
        # not an inline cancel. Spec §4 ("pre-pick").
        OrderState.PICKING: frozenset({OrderState.PICKED}),
        OrderState.PICKED: frozenset({OrderState.PACKED}),
        OrderState.PACKED: frozenset({OrderState.SHIPPED}),
        OrderState.SHIPPED: frozenset({OrderState.CLOSED}),
        OrderState.CLOSED: frozenset(),
        OrderState.CANCELLED: frozenset(),
    },
)

RESERVATION_MACHINE: StateMachine[ReservationState] = StateMachine(
    name="reservation",
    transitions={
        # held is the only non-terminal state: a reservation is consumed by a
        # pick, released by an explicit cancel, or expired by the TTL reaper.
        ReservationState.HELD: frozenset(
            {
                ReservationState.CONSUMED,
                ReservationState.RELEASED,
                ReservationState.EXPIRED,
            }
        ),
        ReservationState.CONSUMED: frozenset(),
        ReservationState.RELEASED: frozenset(),
        ReservationState.EXPIRED: frozenset(),
    },
)
