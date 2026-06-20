"""Unit tests for the arrive handler (pure state CAS, no DB)."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

import pytest

from quartermaster.application.commands import ArriveCommand
from quartermaster.application.errors import OccConflict
from quartermaster.application.handlers.arrive import arrive
from quartermaster.application.results import ArriveResult
from quartermaster.domain.errors import IllegalTransition, ReceiptNotFound
from quartermaster.domain.ids import IdempotencyKey, ReceiptId
from quartermaster.domain.receipts import Receipt, ReceiptKind
from quartermaster.domain.state_machines import ReceiptState
from tests.unit.fakes import FakeReceiptRepo, FakeUnitOfWork

RID = ReceiptId(UUID("00000000-0000-7000-8000-000000000005"))
KEY = IdempotencyKey("k")


def _receipt(state: ReceiptState, version: int = 1) -> Receipt:
    return Receipt(
        RID, ReceiptKind.SUPPLIER_RECEIPT, state, version, datetime(2026, 6, 20, tzinfo=UTC), None
    )


async def _run(uow: FakeUnitOfWork) -> ArriveResult:
    return await arrive(uow, ArriveCommand(RID, KEY))


async def test_arrive_advances_expected_to_arrived() -> None:
    receipts = FakeReceiptRepo(receipt=_receipt(ReceiptState.EXPECTED))
    result = await _run(FakeUnitOfWork(receipts=receipts))
    assert result.state is ReceiptState.ARRIVED
    assert receipts.cas_calls == [(RID, ReceiptState.EXPECTED, 1, ReceiptState.ARRIVED)]


async def test_arrive_missing_receipt_raises_not_found() -> None:
    with pytest.raises(ReceiptNotFound):
        await _run(FakeUnitOfWork(receipts=FakeReceiptRepo(receipt=None)))


async def test_arrive_from_received_raises_illegal_transition() -> None:
    with pytest.raises(IllegalTransition):
        await _run(
            FakeUnitOfWork(receipts=FakeReceiptRepo(receipt=_receipt(ReceiptState.RECEIVED)))
        )


async def test_arrive_cas_conflict_raises_occ() -> None:
    receipts = FakeReceiptRepo(receipt=_receipt(ReceiptState.EXPECTED), cas_result=False)
    with pytest.raises(OccConflict):
        await _run(FakeUnitOfWork(receipts=receipts))
