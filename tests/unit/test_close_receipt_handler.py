"""Unit tests for the close handler (pure state CAS, no DB)."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

import pytest

from quartermaster.application.commands import CloseReceiptCommand
from quartermaster.application.errors import OccConflict
from quartermaster.application.handlers.close_receipt import close_receipt
from quartermaster.application.results import CloseReceiptResult
from quartermaster.domain.errors import IllegalTransition, ReceiptNotFound
from quartermaster.domain.ids import IdempotencyKey, ReceiptId
from quartermaster.domain.receipts import Receipt, ReceiptKind
from quartermaster.domain.state_machines import ReceiptState
from tests.unit.fakes import FakeReceiptRepo, FakeUnitOfWork

RID = ReceiptId(UUID("00000000-0000-7000-8000-000000000005"))
KEY = IdempotencyKey("k")


def _receipt(state: ReceiptState, version: int = 4) -> Receipt:
    return Receipt(
        RID, ReceiptKind.SUPPLIER_RECEIPT, state, version, datetime(2026, 6, 20, tzinfo=UTC), None
    )


async def _run(uow: FakeUnitOfWork) -> CloseReceiptResult:
    return await close_receipt(uow, CloseReceiptCommand(RID, KEY))


async def test_close_advances_putaway_complete_to_closed() -> None:
    receipts = FakeReceiptRepo(receipt=_receipt(ReceiptState.PUTAWAY_COMPLETE))
    result = await _run(FakeUnitOfWork(receipts=receipts))
    assert result.state is ReceiptState.CLOSED
    assert receipts.cas_calls == [(RID, ReceiptState.PUTAWAY_COMPLETE, 4, ReceiptState.CLOSED)]


async def test_close_missing_receipt_raises_not_found() -> None:
    with pytest.raises(ReceiptNotFound):
        await _run(FakeUnitOfWork(receipts=FakeReceiptRepo(receipt=None)))


async def test_close_from_received_raises_illegal_transition() -> None:
    with pytest.raises(IllegalTransition):
        await _run(
            FakeUnitOfWork(receipts=FakeReceiptRepo(receipt=_receipt(ReceiptState.RECEIVED)))
        )


async def test_close_cas_conflict_raises_occ() -> None:
    receipts = FakeReceiptRepo(receipt=_receipt(ReceiptState.PUTAWAY_COMPLETE), cas_result=False)
    with pytest.raises(OccConflict):
        await _run(FakeUnitOfWork(receipts=receipts))
