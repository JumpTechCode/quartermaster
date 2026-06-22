"""The three stock-decrement strategies for the comparative harness run.

Only ``reserve_up_to`` differs across them; the handler, envelope, movement
append, and oracle are identical (design spec §2-§3). ``naive`` is the
absolute-value lost update (slips past every ``stock`` CHECK, caught only by the
oracle); ``read_cas`` is a value-CAS that raises ``OccConflict`` on a lost race so
the envelope retries (correct, but thrashes); ``guarded`` is the production
``_RESERVE_UP_TO`` primitive (correct and fast).
"""

from __future__ import annotations

from collections.abc import Callable

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine

from quartermaster.adapters.postgres.tables import stock
from quartermaster.adapters.postgres.unit_of_work import PgStockRepo, PostgresUnitOfWork
from quartermaster.application.errors import OccConflict
from quartermaster.application.ports import UnitOfWork, UnitOfWorkFactory
from quartermaster.domain.ids import LocationId, SkuId


class NaiveStockRepo(PgStockRepo):
    """Read-modify-write with an ABSOLUTE write and no guard — the bug."""

    async def reserve_up_to(self, sku: SkuId, location: LocationId, want: int) -> int:
        row = (
            await self._conn.execute(
                select(stock.c.qty_on_hand, stock.c.qty_reserved).where(
                    stock.c.sku_id == sku, stock.c.location_id == location
                )
            )
        ).first()
        if row is None:
            return 0
        take = min(want, int(row.qty_on_hand) - int(row.qty_reserved))
        if take <= 0:
            return 0
        # Absolute value, no WHERE guard: each write lands in bounds, so all three
        # stock CHECKs accept it and the lost update is invisible to the storage
        # layer -- only the oracle's conservation_reserved catches it.
        await self._conn.execute(
            stock.update()
            .where(stock.c.sku_id == sku, stock.c.location_id == location)
            .values(qty_reserved=int(row.qty_reserved) + take)
        )
        return take


class CasStockRepo(PgStockRepo):
    """Read, then compare-and-swap on the observed values — correct, but thrashes."""

    async def reserve_up_to(self, sku: SkuId, location: LocationId, want: int) -> int:
        row = (
            await self._conn.execute(
                select(stock.c.qty_on_hand, stock.c.qty_reserved).where(
                    stock.c.sku_id == sku, stock.c.location_id == location
                )
            )
        ).first()
        if row is None:
            return 0
        on_hand, reserved = int(row.qty_on_hand), int(row.qty_reserved)
        take = min(want, on_hand - reserved)
        if take <= 0:
            return 0
        result = await self._conn.execute(
            stock.update()
            .where(
                stock.c.sku_id == sku,
                stock.c.location_id == location,
                stock.c.qty_reserved == reserved,
                stock.c.qty_on_hand == on_hand,
            )
            .values(qty_reserved=reserved + take)
        )
        if result.rowcount != 1:
            raise OccConflict(f"stock CAS lost for {sku} @ {location}")
        return take


def guarded_uow_factory(engine: AsyncEngine) -> UnitOfWorkFactory:
    """Production strategy: the unchanged invariant-guarded primitive."""

    def factory() -> UnitOfWork:
        return PostgresUnitOfWork(engine)

    return factory


def naive_uow_factory(engine: AsyncEngine) -> UnitOfWorkFactory:
    def factory() -> UnitOfWork:
        return PostgresUnitOfWork(engine, stock_repo_factory=NaiveStockRepo)

    return factory


def cas_uow_factory(engine: AsyncEngine) -> UnitOfWorkFactory:
    def factory() -> UnitOfWork:
        return PostgresUnitOfWork(engine, stock_repo_factory=CasStockRepo)

    return factory


STRATEGIES: dict[str, Callable[[AsyncEngine], UnitOfWorkFactory]] = {
    "naive": naive_uow_factory,
    "read_cas": cas_uow_factory,
    "guarded": guarded_uow_factory,
}
