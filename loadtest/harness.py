"""Harness orchestration: drive one strategy, then audit with the oracle.

Each strategy runs against a freshly truncated + reseeded store, then the offline
invariant oracle (REPEATABLE READ snapshot) audits the quiesced result. ``oversell``
is the total magnitude of oracle discrepancies — units of stock the ledger and the
live tables disagree on (design spec §8).
"""

from __future__ import annotations

import random
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncEngine

from loadtest.metrics import StrategyMetrics, summarize
from loadtest.runner import drive
from loadtest.strategies import STRATEGIES, guarded_uow_factory
from loadtest.workload import (
    allocate_thunk,
    chain_thunk,
    seed_comparative,
    seed_mixed,
    truncate_all,
)
from quartermaster.adapters.postgres.identifiers import new_order_id
from quartermaster.adapters.postgres.tables import (
    location,
    movement,
    order_line,
    orders,
    reservation,
    sku,
    stock,
)
from quartermaster.adapters.postgres.unit_of_work import postgres_read_uow_factory
from quartermaster.application.oracle import OracleReport, run_oracle
from quartermaster.domain.ids import IdempotencyKey
from quartermaster.domain.state_machines import OrderState


@dataclass(frozen=True)
class StrategyReport:
    """One strategy's metrics, its oracle audit, and the oversell magnitude."""

    metrics: StrategyMetrics
    oracle: OracleReport
    oversell: int


def violation_magnitude(report: OracleReport) -> int:
    """Sigma |expected - actual| across every discrepancy in every check."""
    return sum(abs(d.expected - d.actual) for c in report.checks for d in c.discrepancies)


async def run_strategy(
    engine: AsyncEngine,
    *,
    strategy: str,
    seed: int,
    n_skus: int,
    n_orders: int,
    on_hand: int,
    qty: int,
    concurrency: int,
    dup: int,
) -> StrategyReport:
    """Truncate, seed, drive ``strategy`` under contention, then audit."""
    await truncate_all(engine)
    rng = random.Random(seed)
    seeded = await seed_comparative(
        engine,
        n_skus=n_skus,
        n_orders=n_orders,
        on_hand_per_cell=on_hand,
        qty_per_order=qty,
        rng=rng,
    )
    uow_factory = STRATEGIES[strategy](engine)
    # dup duplicates share the order's key, so the idempotency layer dedups them:
    # duplicate injection that exercises exactly-once inside the storm (dup=1: none).
    thunks = [
        allocate_thunk(uow_factory, oid, IdempotencyKey(f"{strategy}-{i}"))
        for i, oid in enumerate(seeded.order_ids)
        for _ in range(dup)
    ]
    samples, wall = await drive(thunks, concurrency=concurrency, rand=rng.random)
    metrics = summarize(strategy, samples, wall)
    oracle = await run_oracle(postgres_read_uow_factory(engine))
    return StrategyReport(metrics=metrics, oracle=oracle, oversell=violation_magnitude(oracle))


async def comparative_sweep(
    engine: AsyncEngine,
    *,
    seed: int,
    n_skus: int,
    n_orders: int,
    on_hand: int,
    qty: int,
    concurrency: int,
    dup: int,
) -> list[StrategyReport]:
    """Run all three strategies on the identical workload, in narrative order."""
    return [
        await run_strategy(
            engine,
            strategy=name,
            seed=seed,
            n_skus=n_skus,
            n_orders=n_orders,
            on_hand=on_hand,
            qty=qty,
            concurrency=concurrency,
            dup=dup,
        )
        for name in ("naive", "read_cas", "guarded")
    ]


@dataclass(frozen=True)
class ExactlyOnceResult:
    """Witnesses that K concurrent fires of one key applied exactly once."""

    reserved: int
    movement_rows: int
    reservation_rows: int


async def assert_exactly_once(engine: AsyncEngine, *, k: int, qty: int) -> ExactlyOnceResult:
    """Fire one idempotency key ``k`` times concurrently; read the single effect.

    The oracle reports ``exactly_once = NOT_CHECKED`` because conservation cannot
    witness a lockstep double-apply (ADR-0023); this asserts it directly, as the
    oracle module docstring prescribes (design spec §7.3).
    """
    await truncate_all(engine)
    order_id = new_order_id()
    async with engine.begin() as conn:
        await conn.execute(sku.insert().values(sku_id="ONCE", description="x", unit="each"))
        await conn.execute(location.insert().values(location_id="S0", kind="shelf"))
        await conn.execute(
            stock.insert().values(sku_id="ONCE", location_id="S0", qty_on_hand=qty, qty_reserved=0)
        )
        await conn.execute(
            orders.insert().values(
                order_id=order_id,
                state=OrderState.CREATED.value,
                version=1,
                created_at=datetime.now(UTC),
            )
        )
        await conn.execute(
            order_line.insert().values(
                order_id=order_id,
                sku_id="ONCE",
                ordered_qty=qty,
                allocated_qty=0,
                picked_qty=0,
                shipped_qty=0,
            )
        )
    uow_factory = guarded_uow_factory(engine)
    key = IdempotencyKey("once")
    thunks = [allocate_thunk(uow_factory, order_id, key) for _ in range(k)]
    await drive(thunks, concurrency=k, rand=random.Random(0).random)
    async with engine.connect() as conn:
        reserved = (
            await conn.execute(select(stock.c.qty_reserved).where(stock.c.sku_id == "ONCE"))
        ).scalar_one()
        movement_rows = (
            await conn.execute(
                select(func.count()).select_from(movement).where(movement.c.command_id == "once")
            )
        ).scalar_one()
        reservation_rows = (
            await conn.execute(
                select(func.count())
                .select_from(reservation)
                .where(reservation.c.order_id == order_id)
            )
        ).scalar_one()
    return ExactlyOnceResult(
        reserved=int(reserved),
        movement_rows=int(movement_rows),
        reservation_rows=int(reservation_rows),
    )


async def run_mixed(
    engine: AsyncEngine,
    *,
    seed: int,
    n_chains: int,
    qty: int,
    concurrency: int,
    dup_fraction: float,
    return_fraction: float,
) -> tuple[StrategyMetrics, OracleReport]:
    """Drive ``n_chains`` full document chains concurrently under the production
    strategy, with seeded duplicate-injection and RMA fractions; then audit.

    Retries are not instrumented here (the ``run_*`` wrappers use the real sleep,
    not the counting one) — mixed mode proves the full invariant set + throughput,
    not the thrash metric, which is the comparative run's job (design spec §7.2).
    """
    await truncate_all(engine)
    rng = random.Random(seed)
    seeded = await seed_mixed(engine, n_chains=n_chains, qty=qty, rng=rng)
    uow_factory = guarded_uow_factory(engine)
    thunks = [
        chain_thunk(
            uow_factory,
            chain,
            idx=i,
            dup_step=(rng.random() < dup_fraction),
            do_return=(rng.random() < return_fraction),
        )
        for i, chain in enumerate(seeded.chains)
    ]
    samples, wall = await drive(thunks, concurrency=concurrency, rand=rng.random)
    return summarize("mixed", samples, wall), await run_oracle(postgres_read_uow_factory(engine))
