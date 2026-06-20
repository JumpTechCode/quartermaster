"""The record-only fakes structurally satisfy the ports."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

from quartermaster.application.ports import ClaimOutcome, UnitOfWork
from quartermaster.domain.ids import LocationId, ReceiptId, SkuId
from quartermaster.domain.receipts import Receipt, ReceiptKind, ReceiptLine
from quartermaster.domain.state_machines import ReceiptState
from tests.unit.fakes import FakeCatalogRepo, FakeReceiptRepo, FakeStockRepo, FakeUnitOfWork

_RID = ReceiptId(UUID("00000000-0000-7000-8000-000000000005"))


async def test_fake_uow_satisfies_the_protocol() -> None:
    uow: UnitOfWork = FakeUnitOfWork()  # assignment is the structural check
    async with uow as entered:
        await entered.commit()
    assert isinstance(uow, FakeUnitOfWork)


async def test_fake_stock_reserve_up_to_is_partial() -> None:
    repo = FakeStockRepo({(SkuId("S"), LocationId("L")): 3})
    assert await repo.reserve_up_to(SkuId("S"), LocationId("L"), 5) == 3
    assert await repo.reserve_up_to(SkuId("S"), LocationId("L"), 5) == 0


def test_claim_outcome_has_two_members() -> None:
    assert {ClaimOutcome.CLAIMED, ClaimOutcome.EXISTS} == set(ClaimOutcome)


async def test_fake_receipt_repo_records_and_returns() -> None:
    receipt = Receipt(
        _RID,
        ReceiptKind.SUPPLIER_RECEIPT,
        ReceiptState.EXPECTED,
        1,
        datetime(2026, 6, 20, tzinfo=UTC),
        None,
    )
    line = ReceiptLine(_RID, SkuId("A"), 5, 0)
    repo = FakeReceiptRepo(receipt=receipt, lines=[line])
    assert await repo.get(_RID) is receipt
    assert await repo.get_lines(_RID) == [line]
    await repo.insert_receipt(receipt, [line])
    assert repo.inserted == [(receipt, [line])]
    assert await repo.cas_state(_RID, ReceiptState.EXPECTED, 1, ReceiptState.ARRIVED)
    assert repo.cas_calls == [(_RID, ReceiptState.EXPECTED, 1, ReceiptState.ARRIVED)]
    assert await repo.add_received(_RID, SkuId("A"), 2)
    assert repo.received == [(_RID, SkuId("A"), 2)]


async def test_fake_stock_add_on_hand_accumulates() -> None:
    stock = FakeStockRepo()
    await stock.add_on_hand(SkuId("A"), LocationId("L1"), 3)
    await stock.add_on_hand(SkuId("A"), LocationId("L1"), 2)
    assert stock.received_calls == [
        (SkuId("A"), LocationId("L1"), 3),
        (SkuId("A"), LocationId("L1"), 2),
    ]
    assert stock.cells[(SkuId("A"), LocationId("L1"))] == 5


async def test_fake_catalog_location_exists() -> None:
    catalog = FakeCatalogRepo(known_locations={LocationId("L1")})
    assert await catalog.location_exists(LocationId("L1"))
    assert not await catalog.location_exists(LocationId("L2"))
