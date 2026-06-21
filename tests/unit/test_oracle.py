"""Unit tests for the offline invariant oracle's pure logic (no database).

Each check is isolated here because build_report() takes its four inputs
independently; a real CHECK-constrained store couples them (see the spec §8 note
that no_oversell cannot fail unless conservation/monotonicity also fail).
"""

from __future__ import annotations

from uuid import UUID

from quartermaster.application.oracle import (
    CheckStatus,
    build_report,
    reconstruct,
    run_oracle,
)
from quartermaster.application.ports import LineQuantities, MovementTotal, StockCell
from quartermaster.domain.ids import LocationId, OrderId, SkuId
from quartermaster.domain.movements import MovementType
from tests.unit.fakes import (
    FakeMovementRepo,
    FakeOrderRepo,
    FakeStockRepo,
    FakeUnitOfWork,
    fake_factory,
)

S = SkuId("S")
A1 = LocationId("A1")
DOCK = LocationId("DOCK")
_OID = OrderId(UUID("00000000-0000-7000-8000-000000000001"))


def _mt(
    type_: MovementType,
    qty: int,
    *,
    frm: LocationId | None = None,
    to: LocationId | None = None,
) -> MovementTotal:
    return MovementTotal(type=type_, sku_id=S, from_location=frm, to_location=to, total_qty=qty)


# A balanced store: receive 10 to DOCK, putaway 10 DOCK->A1, reserve 4 at A1, pick 4 at A1.
# A1: on_hand = putaway_to(10) - pick_from(4) = 6 ; reserved = reserve(4) - pick(4) = 0
# DOCK: on_hand = receive_to(10) - putaway_from(10) = 0 ; reserved = 0
def _balanced_totals() -> list[MovementTotal]:
    return [
        _mt(MovementType.RECEIVE, 10, to=DOCK),
        _mt(MovementType.PUTAWAY, 10, frm=DOCK, to=A1),
        _mt(MovementType.RESERVE, 4, to=A1),
        _mt(MovementType.PICK, 4, frm=A1),
    ]


def _balanced_cells() -> list[StockCell]:
    return [
        StockCell(sku_id=S, location_id=A1, on_hand=6, reserved=0),
        StockCell(sku_id=S, location_id=DOCK, on_hand=0, reserved=0),
    ]


def _balanced_shipped() -> dict[SkuId, int]:
    return {S: 4}  # picked 4, shipped 4


def test_reconstruct_balanced() -> None:
    on_hand, reserved = reconstruct(_balanced_totals())
    assert on_hand == {(S, A1): 6, (S, DOCK): 0}
    assert reserved == {(S, A1): 0}


def test_balanced_store_all_ok() -> None:
    report = build_report(_balanced_totals(), _balanced_cells(), _balanced_shipped(), [])
    assert report.ok
    assert report.check("conservation_on_hand").status is CheckStatus.OK
    assert report.check("conservation_reserved").status is CheckStatus.OK
    assert report.check("no_oversell").status is CheckStatus.OK
    assert report.check("stock_bounds").status is CheckStatus.OK
    assert report.check("state_integrity").status is CheckStatus.OK
    assert report.check("exactly_once").status is CheckStatus.NOT_CHECKED


def test_conservation_on_hand_cell_off() -> None:
    cells = [StockCell(S, A1, 7, 0), StockCell(S, DOCK, 0, 0)]  # A1 on_hand 7, ledger says 6
    report = build_report(_balanced_totals(), cells, _balanced_shipped(), [])
    check = report.check("conservation_on_hand")
    assert check.status is CheckStatus.FAILED
    d = check.discrepancies[0]
    assert (d.sku_id, d.location_id, d.expected, d.actual) == (S, A1, 6, 7)
    assert report.check("conservation_reserved").status is CheckStatus.OK
    assert not report.ok


def test_conservation_on_hand_stock_without_ledger() -> None:
    cells = [*_balanced_cells(), StockCell(S, LocationId("GHOST"), 3, 0)]
    report = build_report(_balanced_totals(), cells, _balanced_shipped(), [])
    check = report.check("conservation_on_hand")
    assert check.status is CheckStatus.FAILED
    ghost = next(d for d in check.discrepancies if d.location_id == LocationId("GHOST"))
    assert (ghost.expected, ghost.actual) == (0, 3)


def test_conservation_on_hand_ledger_without_stock() -> None:
    totals = [*_balanced_totals(), _mt(MovementType.RECEIVE, 5, to=LocationId("PHANTOM"))]
    report = build_report(totals, _balanced_cells(), _balanced_shipped(), [])
    check = report.check("conservation_on_hand")
    assert check.status is CheckStatus.FAILED
    phantom = next(d for d in check.discrepancies if d.location_id == LocationId("PHANTOM"))
    assert (phantom.expected, phantom.actual) == (5, 0)


def test_conservation_reserved_cell_off() -> None:
    cells = [StockCell(S, A1, 6, 1), StockCell(S, DOCK, 0, 0)]  # reserved 1, ledger says 0
    report = build_report(_balanced_totals(), cells, _balanced_shipped(), [])
    assert report.check("conservation_reserved").status is CheckStatus.FAILED
    assert report.check("conservation_on_hand").status is CheckStatus.OK


def test_no_oversell_violation_isolated() -> None:
    # cells/ledger agree (conservation OK) but shipped exceeds ever_received.
    report = build_report(_balanced_totals(), _balanced_cells(), {S: 100}, [])
    over = report.check("no_oversell")
    assert over.status is CheckStatus.FAILED
    d = over.discrepancies[0]
    # ever_received = 10 ; shipped + on_hand_total = 100 + 6
    assert (d.sku_id, d.expected, d.actual) == (S, 10, 106)
    assert report.check("conservation_on_hand").status is CheckStatus.OK


def test_stock_bounds_reserved_exceeds_on_hand() -> None:
    # Craft ledger to MATCH the bad cell so only stock_bounds fails.
    totals = [_mt(MovementType.RECEIVE, 2, to=A1), _mt(MovementType.RESERVE, 5, to=A1)]
    cells = [StockCell(S, A1, 2, 5)]  # reserved 5 > on_hand 2
    report = build_report(totals, cells, {}, [])
    bounds = report.check("stock_bounds")
    assert bounds.status is CheckStatus.FAILED
    assert bounds.discrepancies[0].location_id == A1
    assert report.check("conservation_on_hand").status is CheckStatus.OK
    assert report.check("conservation_reserved").status is CheckStatus.OK


def test_state_integrity_bad_line() -> None:
    bad = LineQuantities(order_id=_OID, sku_id=S, ordered=5, allocated=5, picked=5, shipped=6)
    report = build_report(_balanced_totals(), _balanced_cells(), _balanced_shipped(), [bad])
    si = report.check("state_integrity")
    assert si.status is CheckStatus.FAILED
    assert si.discrepancies[0].sku_id == S
    assert not report.ok


def test_report_check_unknown_raises() -> None:
    report = build_report(_balanced_totals(), _balanced_cells(), _balanced_shipped(), [])
    try:
        report.check("nope")
    except KeyError:
        pass
    else:  # pragma: no cover
        raise AssertionError("expected KeyError")


async def test_run_oracle_over_fakes_all_ok() -> None:
    uow = FakeUnitOfWork(
        stock=FakeStockRepo(all_cells=_balanced_cells()),
        orders=FakeOrderRepo(shipped_totals=_balanced_shipped(), monotonic_violations=[]),
        movements=FakeMovementRepo(totals=_balanced_totals()),
    )
    report = await run_oracle(fake_factory(uow))
    assert report.ok


async def test_run_oracle_over_fakes_detects_drift() -> None:
    uow = FakeUnitOfWork(
        stock=FakeStockRepo(all_cells=[StockCell(S, A1, 99, 0), StockCell(S, DOCK, 0, 0)]),
        orders=FakeOrderRepo(shipped_totals=_balanced_shipped(), monotonic_violations=[]),
        movements=FakeMovementRepo(totals=_balanced_totals()),
    )
    report = await run_oracle(fake_factory(uow))
    assert not report.ok
    assert report.check("conservation_on_hand").status is CheckStatus.FAILED
