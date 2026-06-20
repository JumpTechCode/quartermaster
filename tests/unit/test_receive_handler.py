"""Unit tests for the receive handler's CAS-gated stock landing (no DB)."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from itertools import count
from uuid import UUID

import pytest

from quartermaster.application.commands import ReceiveCommand
from quartermaster.application.errors import OccConflict
from quartermaster.application.handlers.receive import receive
from quartermaster.application.results import ReceiveResult
from quartermaster.domain.errors import (
    IllegalTransition,
    InvalidReceiptLine,
    InvariantViolation,
    ReceiptNotFound,
    UnknownLocation,
)
from quartermaster.domain.ids import IdempotencyKey, LocationId, MovementId, ReceiptId, SkuId
from quartermaster.domain.movements import MovementType
from quartermaster.domain.receipts import Receipt, ReceiptKind, ReceiptLine
from quartermaster.domain.state_machines import ReceiptState
from tests.unit.fakes import (
    FakeCatalogRepo,
    FakeMovementRepo,
    FakeReceiptRepo,
    FakeStockRepo,
    FakeUnitOfWork,
)

RID = ReceiptId(UUID("00000000-0000-7000-8000-000000000005"))
LOC = LocationId("RCV")
KEY = IdempotencyKey("k")


def _now() -> datetime:
    return datetime(2026, 6, 20, 12, 0, tzinfo=UTC)


def _mov_ids() -> Callable[[], MovementId]:
    counter = count(1)
    return lambda: MovementId(UUID(int=(0xCC << 64) | next(counter)))


def _receipt(state: ReceiptState = ReceiptState.ARRIVED, version: int = 1) -> Receipt:
    return Receipt(RID, ReceiptKind.SUPPLIER_RECEIPT, state, version, _now(), None)


def _line(sku: str, expected: int, received: int = 0) -> ReceiptLine:
    return ReceiptLine(RID, SkuId(sku), expected, received)


def _harness(
    *,
    receipt: Receipt | None,
    lines: list[ReceiptLine],
    cas_result: bool = True,
    add_received_result: bool = True,
    known_locations: frozenset[LocationId] = frozenset({LOC}),
) -> tuple[FakeUnitOfWork, FakeReceiptRepo, FakeStockRepo, FakeMovementRepo]:
    receipts = FakeReceiptRepo(
        receipt=receipt, lines=lines, cas_result=cas_result, add_received_result=add_received_result
    )
    stock = FakeStockRepo()
    movements = FakeMovementRepo()
    catalog = FakeCatalogRepo(known_locations=set(known_locations))
    uow = FakeUnitOfWork(receipts=receipts, stock=stock, movements=movements, catalog=catalog)
    return uow, receipts, stock, movements


async def _run(
    uow: FakeUnitOfWork, lines: tuple[tuple[SkuId, int], ...] = ((SkuId("A"), 5),)
) -> ReceiveResult:
    return await receive(
        uow, ReceiveCommand(RID, LOC, lines, KEY), now=_now, new_movement_id=_mov_ids()
    )


async def test_receive_lands_stock_and_advances() -> None:
    uow, receipts, stock, movements = _harness(receipt=_receipt(), lines=[_line("A", 5)])
    result = await _run(uow)

    assert result.state is ReceiptState.RECEIVED
    assert [(line.sku_id, line.received) for line in result.lines] == [("A", 5)]
    assert receipts.received == [(RID, SkuId("A"), 5)]
    assert stock.received_calls == [(SkuId("A"), LOC, 5)]
    mv = movements.appended[0]
    assert mv.type is MovementType.RECEIVE and mv.from_location is None
    assert mv.to_location == LOC and mv.qty == 5 and mv.ref == RID
    assert [(c[1], c[2], c[3]) for c in receipts.cas_calls] == [
        (ReceiptState.ARRIVED, 1, ReceiptState.RECEIVING),
        (ReceiptState.RECEIVING, 2, ReceiptState.RECEIVED),
    ]


async def test_receive_partial_short_shipment_still_advances() -> None:
    uow, receipts, stock, _ = _harness(receipt=_receipt(), lines=[_line("A", 10)])
    result = await _run(uow, lines=((SkuId("A"), 4),))

    assert result.state is ReceiptState.RECEIVED
    assert receipts.received == [(RID, SkuId("A"), 4)]
    assert stock.received_calls == [(SkuId("A"), LOC, 4)]


async def test_receive_unknown_location_rejected() -> None:
    uow, *_ = _harness(receipt=_receipt(), lines=[_line("A", 5)], known_locations=frozenset())
    with pytest.raises(UnknownLocation):
        await _run(uow)


async def test_receive_unknown_line_rejected() -> None:
    uow, *_ = _harness(receipt=_receipt(), lines=[_line("A", 5)])
    with pytest.raises(InvalidReceiptLine):
        await _run(uow, lines=((SkuId("B"), 1),))


async def test_receive_over_expected_rejected() -> None:
    uow, *_ = _harness(receipt=_receipt(), lines=[_line("A", 5, received=3)])
    with pytest.raises(InvalidReceiptLine):
        await _run(uow, lines=((SkuId("A"), 3),))  # 3 already received + 3 > 5 expected


async def test_receive_missing_receipt_raises_not_found() -> None:
    uow, *_ = _harness(receipt=None, lines=[])
    with pytest.raises(ReceiptNotFound):
        await _run(uow)


async def test_receive_from_expected_raises_illegal_transition() -> None:
    uow, *_ = _harness(receipt=_receipt(state=ReceiptState.EXPECTED), lines=[_line("A", 5)])
    with pytest.raises(IllegalTransition):
        await _run(uow)


async def test_receive_cas_conflict_raises_occ() -> None:
    uow, *_ = _harness(receipt=_receipt(), lines=[_line("A", 5)], cas_result=False)
    with pytest.raises(OccConflict):
        await _run(uow)


async def test_receive_add_received_miss_after_cas_raises_invariant() -> None:
    uow, *_ = _harness(receipt=_receipt(), lines=[_line("A", 5)], add_received_result=False)
    with pytest.raises(InvariantViolation):
        await _run(uow)
