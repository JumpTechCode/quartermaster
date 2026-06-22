"""The offline invariant oracle (design spec §7, ADR-0011, ADR-0023).

A read-only, post-run audit. It reconstructs on-hand and reserved stock per
``(sku, location)`` cell from the append-only movement ledger and checks them
against the live ``stock`` and ``order_line`` tables, surfacing lost-update /
double-apply bugs the storage CHECK constraints cannot catch. It never runs on
the command path: no envelope, no idempotency, no commit.

The reconstruction is a pure function of the ledger's (type, from, to, qty)
totals via this type->effect mapping:

    on_hand  += qty at ``to``  for RECEIVE, PUTAWAY ;  -= qty at ``from`` for PUTAWAY, PICK
    reserved += qty at ``to``  for RESERVE          ;  -= qty at ``from`` for RELEASE, EXPIRE, PICK

Keeping that arithmetic here (not in SQL) makes it exhaustively unit-testable
without a database; the adapter does only the GROUP BY totalling.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass, field
from enum import Enum, auto

from quartermaster.application.ports import (
    LineQuantities,
    MovementTotal,
    StockCell,
    UnitOfWorkFactory,
)
from quartermaster.domain.ids import LocationId, SkuId
from quartermaster.domain.movements import MovementType

Cell = tuple[SkuId, LocationId]


class CheckStatus(Enum):
    """Outcome of one oracle check."""

    OK = auto()
    FAILED = auto()
    NOT_CHECKED = auto()


@dataclass(frozen=True)
class Discrepancy:
    """One mismatch: at ``(sku_id, location_id)`` the oracle expected vs. observed."""

    sku_id: SkuId
    location_id: LocationId | None
    expected: int
    actual: int
    detail: str


@dataclass(frozen=True)
class CheckResult:
    """The outcome of a single named check and any discrepancies it found."""

    name: str
    status: CheckStatus
    discrepancies: tuple[Discrepancy, ...] = ()


@dataclass(frozen=True)
class OracleReport:
    """The result of one oracle run: one CheckResult per invariant."""

    checks: tuple[CheckResult, ...] = field(default=())

    @property
    def ok(self) -> bool:
        """True iff no check FAILED (a NOT_CHECKED check does not fail the report)."""
        return all(c.status is not CheckStatus.FAILED for c in self.checks)

    def check(self, name: str) -> CheckResult:
        """The CheckResult with ``name``; raises KeyError if absent."""
        for c in self.checks:
            if c.name == name:
                return c
        raise KeyError(name)


# type -> effect on the per-cell balances.
_ON_HAND_ADD = frozenset({MovementType.RECEIVE, MovementType.PUTAWAY})  # at `to`
_ON_HAND_SUB = frozenset({MovementType.PUTAWAY, MovementType.PICK})  # at `from`
_RESERVED_ADD = frozenset({MovementType.RESERVE})  # at `to`
_RESERVED_SUB = frozenset({MovementType.RELEASE, MovementType.EXPIRE, MovementType.PICK})  # `from`


def reconstruct(totals: Iterable[MovementTotal]) -> tuple[dict[Cell, int], dict[Cell, int]]:
    """Fold ledger totals into per-cell ``on_hand`` and ``reserved`` balances."""
    on_hand: dict[Cell, int] = defaultdict(int)
    reserved: dict[Cell, int] = defaultdict(int)
    for t in totals:
        if t.type in _ON_HAND_ADD and t.to_location is not None:
            on_hand[(t.sku_id, t.to_location)] += t.total_qty
        if t.type in _ON_HAND_SUB and t.from_location is not None:
            on_hand[(t.sku_id, t.from_location)] -= t.total_qty
        if t.type in _RESERVED_ADD and t.to_location is not None:
            reserved[(t.sku_id, t.to_location)] += t.total_qty
        if t.type in _RESERVED_SUB and t.from_location is not None:
            reserved[(t.sku_id, t.from_location)] -= t.total_qty
    return dict(on_hand), dict(reserved)


def _conservation(
    name: str, ledger: dict[Cell, int], actual: dict[Cell, int], detail: str
) -> CheckResult:
    discrepancies: list[Discrepancy] = []
    for cell in sorted(set(ledger) | set(actual)):
        expected = ledger.get(cell, 0)
        observed = actual.get(cell, 0)
        if expected != observed:
            sku, loc = cell
            discrepancies.append(Discrepancy(sku, loc, expected, observed, detail))
    status = CheckStatus.OK if not discrepancies else CheckStatus.FAILED
    return CheckResult(name, status, tuple(discrepancies))


def _no_oversell(
    totals: Iterable[MovementTotal], cells: Iterable[StockCell], shipped: dict[SkuId, int]
) -> CheckResult:
    received: dict[SkuId, int] = defaultdict(int)
    for t in totals:
        if t.type is MovementType.RECEIVE:
            received[t.sku_id] += t.total_qty
    on_hand_total: dict[SkuId, int] = defaultdict(int)
    for c in cells:
        on_hand_total[c.sku_id] += c.on_hand
    discrepancies: list[Discrepancy] = []
    for sku in sorted(set(received) | set(on_hand_total) | set(shipped)):
        ever = received.get(sku, 0)
        used = shipped.get(sku, 0) + on_hand_total.get(sku, 0)
        if used > ever:
            discrepancies.append(
                Discrepancy(sku, None, ever, used, "shipped+on_hand exceeds ever_received")
            )
    status = CheckStatus.OK if not discrepancies else CheckStatus.FAILED
    return CheckResult("no_oversell", status, tuple(discrepancies))


def _stock_bounds(cells: Iterable[StockCell]) -> CheckResult:
    discrepancies: list[Discrepancy] = []
    for c in cells:
        if not (0 <= c.reserved <= c.on_hand):
            discrepancies.append(
                Discrepancy(
                    c.sku_id, c.location_id, c.on_hand, c.reserved, "require 0<=reserved<=on_hand"
                )
            )
    status = CheckStatus.OK if not discrepancies else CheckStatus.FAILED
    return CheckResult("stock_bounds", status, tuple(discrepancies))


def _state_integrity(bad_lines: Iterable[LineQuantities]) -> CheckResult:
    discrepancies = tuple(
        Discrepancy(
            line.sku_id,
            None,
            line.ordered,
            line.shipped,
            f"order {line.order_id}: 0<=shipped({line.shipped})<=picked({line.picked})"
            f"<=allocated({line.allocated})<=ordered({line.ordered}) violated",
        )
        for line in bad_lines
    )
    status = CheckStatus.OK if not discrepancies else CheckStatus.FAILED
    return CheckResult("state_integrity", status, discrepancies)


def build_report(
    totals: list[MovementTotal],
    cells: list[StockCell],
    shipped: dict[SkuId, int],
    bad_lines: list[LineQuantities],
) -> OracleReport:
    """Run all checks over already-read store contents (pure; no I/O)."""
    on_hand_ledger, reserved_ledger = reconstruct(totals)
    on_hand_actual = {(c.sku_id, c.location_id): c.on_hand for c in cells}
    reserved_actual = {(c.sku_id, c.location_id): c.reserved for c in cells}
    return OracleReport(
        checks=(
            _conservation("conservation_on_hand", on_hand_ledger, on_hand_actual, "on_hand"),
            _conservation("conservation_reserved", reserved_ledger, reserved_actual, "reserved"),
            _no_oversell(totals, cells, shipped),
            _stock_bounds(cells),
            _state_integrity(bad_lines),
            CheckResult("exactly_once", CheckStatus.NOT_CHECKED),
        )
    )


async def run_oracle(uow_factory: UnitOfWorkFactory) -> OracleReport:
    """Read the store once (no commit) and return the invariant report.

    The four reads cross-check aggregates against each other, so they must fold
    over a single instant. Pass a snapshot-isolated factory
    (``postgres_read_uow_factory``, REPEATABLE READ): under the engine's default
    READ COMMITTED each statement takes a fresh snapshot, so a command committing
    between the ledger read and the stock read yields a torn cross-check -- a
    false FAIL on consistent data, or a real drift masked by a compensating pair
    (issue #70). Run against a quiesced store or under snapshot isolation; never
    on the command path (no envelope, no idempotency, no commit).
    """
    async with uow_factory() as uow:
        totals = await uow.movements.aggregate()
        cells = await uow.stock.all_cells()
        shipped = await uow.orders.shipped_by_sku()
        bad_lines = await uow.orders.lines_breaking_monotonic()
    return build_report(totals, cells, shipped, bad_lines)
