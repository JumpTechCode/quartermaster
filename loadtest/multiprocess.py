"""Multi-process fan-out for the on-demand sweep (design spec §9).

The parent seeds the store and computes the order-id list, then hands disjoint
slices to worker processes — each builds its OWN engine/pool (asyncpg pools are
not fork-safe) and drives guarded allocates for its slice. The parent gathers the
picklable samples and runs the oracle once over the shared result. Worker-local
state only; nothing is shared across the process boundary except plain data.
"""

from __future__ import annotations

import asyncio
import random
from concurrent.futures import ProcessPoolExecutor
from uuid import UUID

from loadtest.harness import StrategyReport, violation_magnitude
from loadtest.metrics import CommandSample, summarize
from loadtest.runner import drive
from loadtest.strategies import guarded_uow_factory
from loadtest.workload import allocate_thunk, seed_comparative, truncate_all
from quartermaster.adapters.postgres.engine import create_engine
from quartermaster.adapters.postgres.unit_of_work import postgres_read_uow_factory
from quartermaster.application.oracle import run_oracle
from quartermaster.domain.ids import IdempotencyKey, OrderId


async def _drive_slice_async(
    database_url: str, order_ids: list[str], key_prefix: str, concurrency: int, seed: int
) -> list[CommandSample]:
    engine = create_engine(database_url)
    try:
        factory = guarded_uow_factory(engine)
        thunks = [
            allocate_thunk(factory, OrderId(UUID(oid)), IdempotencyKey(f"{key_prefix}-{i}"))
            for i, oid in enumerate(order_ids)
        ]
        samples, _ = await drive(thunks, concurrency=concurrency, rand=random.Random(seed).random)
    finally:
        await engine.dispose()
    return samples


def driven_slice(
    database_url: str, order_ids: list[str], key_prefix: str, concurrency: int, seed: int
) -> list[CommandSample]:
    """Process-pool worker entry: run its own event loop over its slice."""
    return asyncio.run(_drive_slice_async(database_url, order_ids, key_prefix, concurrency, seed))


async def multiprocess_sweep(
    database_url: str,
    *,
    seed: int,
    n_skus: int,
    n_orders: int,
    on_hand: int,
    qty: int,
    concurrency: int,
    processes: int,
) -> StrategyReport:
    engine = create_engine(database_url)
    try:
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
        ids = [str(oid) for oid in seeded.order_ids]
        slices = [ids[p::processes] for p in range(processes)]
        loop = asyncio.get_running_loop()
        with ProcessPoolExecutor(max_workers=processes) as pool:
            futures = [
                loop.run_in_executor(
                    pool, driven_slice, database_url, sl, f"mp-{p}", concurrency, seed + p
                )
                for p, sl in enumerate(slices)
            ]
            results = await asyncio.gather(*futures)
        samples = [s for slice_samples in results for s in slice_samples]
        # Wall here is approximate (post-hoc); the comparative table's throughput
        # comes from the single-process run. summarize over the union for tallies.
        metrics = summarize("guarded-mp", samples, wall_seconds=0.0)
        oracle = await run_oracle(postgres_read_uow_factory(engine))
    finally:
        await engine.dispose()
    return StrategyReport(metrics=metrics, oracle=oracle, oversell=violation_magnitude(oracle))
