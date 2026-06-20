"""Unit tests for the putaway handler's CAS-gated stock relocation (no DB)."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from itertools import count
from uuid import UUID

import pytest

from quartermaster.application.commands import PutawayCommand
from quartermaster.application.errors import OccConflict
from quartermaster.application.handlers.putaway import putaway
from quartermaster.application.results import PutawayResult
from quartermaster.domain.errors import (
    IllegalTransition,
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
FROM = LocationId("RCV")
TO = LocationId("A1")
KEY = IdempotencyKey("k")


def _now() -> datetime:
    return datetime(2026, 6, 20, 12, 0, tzinfo=UTC)


def _mov_ids() -> Callable[[], MovementId]:
    counter = count(1)
    return lambda: MovementId(UUID(int=(0xDD << 64) | next(counter)))


def _receipt(state: ReceiptState = ReceiptState.RECEIVED, version: int = 3) -> Receipt:
    return Receipt(RID, ReceiptKind.SUPPLIER_RECEIPT, state, version, _now(), None)


def _line(sku: str, received: int) -> ReceiptLine:
    return ReceiptLine(RID, SkuId(sku), expected=max(received, 1), received=received)


def _harness(
    *,
    receipt: Receipt | None,
    lines: list[ReceiptLine],
    cas_result: bool = True,
    remove_result: bool = True,
    known_locations: frozenset[LocationId] = frozenset({FROM, TO}),
) -> tuple[FakeUnitOfWork, FakeReceiptRepo, FakeStockRepo, FakeMovementRepo]:
    receipts = FakeReceiptRepo(receipt=receipt, lines=lines, cas_result=cas_result)
    stock = FakeStockRepo()
    stock.remove_result = remove_result
    movements = FakeMovementRepo()
    catalog = FakeCatalogRepo(known_locations=set(known_locations))
    uow = FakeUnitOfWork(receipts=receipts, stock=stock, movements=movements, catalog=catalog)
    return uow, receipts, stock, movements


async def _run(uow: FakeUnitOfWork) -> PutawayResult:
    return await putaway(
        uow, PutawayCommand(RID, FROM, TO, KEY), now=_now, new_movement_id=_mov_ids()
    )


async def test_putaway_relocates_each_line_and_advances() -> None:
    uow, receipts, stock, movements = _harness(
        receipt=_receipt(), lines=[_line("A", 5), _line("B", 3)]
    )
    result = await _run(uow)

    assert result.state is ReceiptState.PUTAWAY_COMPLETE
    assert [(line.sku_id, line.moved) for line in result.lines] == [("A", 5), ("B", 3)]
    assert stock.remove_calls == [(SkuId("A"), FROM, 5), (SkuId("B"), FROM, 3)]
    assert stock.received_calls == [
        (SkuId("A"), TO, 5),
        (SkuId("B"), TO, 3),
    ]  # add_on_hand at the shelf
    mv = movements.appended[0]
    assert mv.type is MovementType.PUTAWAY
    assert mv.from_location == FROM and mv.to_location == TO and mv.qty == 5
    assert [(c[1], c[2], c[3]) for c in receipts.cas_calls] == [
        (ReceiptState.RECEIVED, 3, ReceiptState.PUTAWAY_COMPLETE)
    ]


async def test_putaway_skips_zero_received_lines() -> None:
    uow, _receipts, stock, movements = _harness(
        receipt=_receipt(), lines=[_line("A", 5), _line("B", 0)]
    )
    result = await _run(uow)

    assert [(line.sku_id, line.moved) for line in result.lines] == [("A", 5)]
    assert stock.remove_calls == [(SkuId("A"), FROM, 5)]
    assert len(movements.appended) == 1


async def test_putaway_missing_receipt_raises_not_found() -> None:
    uow, *_ = _harness(receipt=None, lines=[])
    with pytest.raises(ReceiptNotFound):
        await _run(uow)


async def test_putaway_from_arrived_raises_illegal_transition() -> None:
    uow, *_ = _harness(receipt=_receipt(state=ReceiptState.ARRIVED), lines=[_line("A", 5)])
    with pytest.raises(IllegalTransition):
        await _run(uow)


async def test_putaway_unknown_from_location_rejected() -> None:
    uow, *_ = _harness(receipt=_receipt(), lines=[_line("A", 5)], known_locations=frozenset({TO}))
    with pytest.raises(UnknownLocation):
        await _run(uow)


async def test_putaway_unknown_to_location_rejected() -> None:
    uow, *_ = _harness(receipt=_receipt(), lines=[_line("A", 5)], known_locations=frozenset({FROM}))
    with pytest.raises(UnknownLocation):
        await _run(uow)


async def test_putaway_remove_miss_after_gate_raises_invariant() -> None:
    uow, *_ = _harness(receipt=_receipt(), lines=[_line("A", 5)], remove_result=False)
    with pytest.raises(InvariantViolation):
        await _run(uow)


async def test_putaway_cas_conflict_raises_occ() -> None:
    uow, *_ = _harness(receipt=_receipt(), lines=[_line("A", 5)], cas_result=False)
    with pytest.raises(OccConflict):
        await _run(uow)
