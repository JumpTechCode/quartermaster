"""The record-only fakes structurally satisfy the ports."""

from __future__ import annotations

from quartermaster.application.ports import ClaimOutcome, UnitOfWork
from quartermaster.domain.ids import LocationId, SkuId
from tests.unit.fakes import FakeStockRepo, FakeUnitOfWork


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
