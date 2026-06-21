"""The record-only fakes structurally satisfy the ports."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

from quartermaster.application.ports import (
    ClaimOutcome,
    LineQuantities,
    MovementTotal,
    StockCell,
    UnitOfWork,
)
from quartermaster.domain.catalog import LocationKind
from quartermaster.domain.ids import LocationId, OrderId, ReceiptId, SkuId
from quartermaster.domain.movements import MovementType
from quartermaster.domain.receipts import Receipt, ReceiptKind, ReceiptLine
from quartermaster.domain.state_machines import ReceiptState
from tests.unit.fakes import (
    FakeCatalogRepo,
    FakeMovementRepo,
    FakeOrderRepo,
    FakeReceiptRepo,
    FakeStockRepo,
    FakeUnitOfWork,
)

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


async def test_fake_catalog_location_kind_from_explicit_map() -> None:
    catalog = FakeCatalogRepo(known_locations={LocationId("L1"): LocationKind.SHELF})
    assert await catalog.location_kind(LocationId("L1")) is LocationKind.SHELF
    assert await catalog.location_kind(LocationId("L2")) is None


async def test_fake_catalog_bare_location_set_defaults_to_non_shelf() -> None:
    catalog = FakeCatalogRepo(known_locations={LocationId("L1")})
    assert await catalog.location_kind(LocationId("L1")) is LocationKind.RECEIVING
    assert await catalog.location_kind(LocationId("L2")) is None


async def test_fake_stock_remove_on_hand_records_and_returns() -> None:
    stock = FakeStockRepo()
    assert await stock.remove_on_hand(SkuId("A"), LocationId("RCV"), 5) is True
    stock.remove_result = False
    assert await stock.remove_on_hand(SkuId("A"), LocationId("RCV"), 5) is False
    assert stock.remove_calls == [
        (SkuId("A"), LocationId("RCV"), 5),
        (SkuId("A"), LocationId("RCV"), 5),
    ]


async def test_fake_order_repo_backordered_orders_respects_limit() -> None:
    from uuid import uuid4

    from quartermaster.domain.ids import OrderId
    from tests.unit.fakes import FakeOrderRepo

    ids = [OrderId(uuid4()) for _ in range(3)]
    repo = FakeOrderRepo(backordered=ids)
    assert await repo.backordered_orders(2) == ids[:2]
    assert repo.backordered_calls == [2]


async def test_fake_order_repo_remove_allocated_and_mark_backordered() -> None:
    from uuid import uuid4

    from quartermaster.domain.ids import OrderId, SkuId
    from tests.unit.fakes import FakeOrderRepo

    oid = OrderId(uuid4())
    repo = FakeOrderRepo()
    assert await repo.remove_allocated(oid, SkuId("S"), 3) is True
    assert await repo.mark_backordered(oid) is True
    assert repo.removed_allocated == [(oid, SkuId("S"), 3)]
    assert repo.mark_backordered_calls == [oid]

    rejecting = FakeOrderRepo(remove_allocated_result=False, mark_backordered_result=False)
    assert await rejecting.remove_allocated(oid, SkuId("S"), 1) is False
    assert await rejecting.mark_backordered(oid) is False


async def test_fake_stock_all_cells_returns_canned() -> None:
    cell = StockCell(sku_id=SkuId("S"), location_id=LocationId("A1"), on_hand=5, reserved=2)
    repo = FakeStockRepo(all_cells=[cell])
    assert await repo.all_cells() == [cell]


async def test_fake_stock_all_cells_defaults_empty() -> None:
    assert await FakeStockRepo().all_cells() == []


async def test_fake_movement_aggregate_returns_canned() -> None:
    total = MovementTotal(
        type=MovementType.RECEIVE,
        sku_id=SkuId("S"),
        from_location=None,
        to_location=LocationId("A1"),
        total_qty=10,
    )
    repo = FakeMovementRepo(totals=[total])
    assert await repo.aggregate() == [total]


async def test_fake_order_shipped_by_sku_and_violations() -> None:
    bad = LineQuantities(
        order_id=OrderId(UUID("00000000-0000-7000-8000-000000000001")),
        sku_id=SkuId("S"),
        ordered=5,
        allocated=5,
        picked=5,
        shipped=6,
    )
    repo = FakeOrderRepo(shipped_totals={SkuId("S"): 4}, monotonic_violations=[bad])
    assert await repo.shipped_by_sku() == {SkuId("S"): 4}
    assert await repo.lines_breaking_monotonic() == [bad]
