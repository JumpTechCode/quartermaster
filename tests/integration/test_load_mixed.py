"""Full document flow under concurrent load stays oracle-green (design spec §7.2)."""

from __future__ import annotations

from loadtest.harness import run_mixed
from sqlalchemy.ext.asyncio import AsyncEngine


async def test_mixed_lifecycle_is_oracle_green(committed_db: AsyncEngine) -> None:
    metrics, oracle = await run_mixed(
        committed_db,
        seed=11,
        n_chains=24,
        qty=3,
        concurrency=12,
        dup_fraction=0.5,
        return_fraction=0.25,
    )
    assert oracle.ok, [c for c in oracle.checks if c.status.name == "FAILED"]
    assert metrics.errors == 0
    assert metrics.count == 24
