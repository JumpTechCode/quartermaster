"""P worker processes drive one guarded workload; the parent audits once."""

from __future__ import annotations

from loadtest.multiprocess import multiprocess_sweep


async def test_multiprocess_guarded_stays_clean(postgres_url: str) -> None:
    report = await multiprocess_sweep(
        postgres_url,
        seed=3,
        n_skus=4,
        n_orders=48,
        on_hand=8,
        qty=1,
        concurrency=16,
        processes=2,
    )
    assert report.oracle.ok
    assert report.oversell == 0
    assert report.metrics.count == 48
    assert report.metrics.errors == 0
