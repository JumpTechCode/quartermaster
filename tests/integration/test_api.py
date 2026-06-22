"""End-to-end HTTP tests on real Postgres: create -> allocate -> read."""

from __future__ import annotations

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine

from quartermaster.adapters.postgres.identifiers import (
    new_movement_id,
    new_order_id,
    new_receipt_id,
    new_reservation_id,
)
from quartermaster.adapters.postgres.tables import reservation
from quartermaster.adapters.postgres.unit_of_work import (
    postgres_read_uow_factory,
    postgres_uow_factory,
)
from quartermaster.api.app import create_app
from quartermaster.api.deps import Deps
from quartermaster.application.clock import system_clock
from tests.integration.seed import assert_invariants, seed_sku_locations_stock


def _client(engine: AsyncEngine) -> httpx.AsyncClient:
    deps = Deps(
        uow_factory=postgres_uow_factory(engine),
        read_uow_factory=postgres_read_uow_factory(engine),
        now=system_clock,
        new_order_id=new_order_id,
        new_receipt_id=new_receipt_id,
        new_reservation_id=new_reservation_id,
        new_movement_id=new_movement_id,
    )
    app = create_app(deps)
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t")


async def test_create_allocate_read_happy_path(committed_db: AsyncEngine) -> None:
    sku = await seed_sku_locations_stock(committed_db, "A", {"L1": 5})
    async with _client(committed_db) as client:
        created = await client.post(
            "/orders",
            json={"lines": [{"sku_id": "A", "qty": 5}]},
            headers={"Idempotency-Key": "c1"},
        )
        assert created.status_code == 201
        order_id = created.json()["order_id"]

        allocated = await client.post(
            f"/orders/{order_id}/allocate", headers={"Idempotency-Key": "a1"}
        )
        assert allocated.status_code == 200
        assert allocated.json()["state"] == "allocated"

        read = await client.get(f"/orders/{order_id}")
        assert read.status_code == 200
        body = read.json()
        assert body["state"] == "allocated"
        assert body["lines"] == [
            {"sku_id": "A", "ordered": 5, "allocated": 5, "picked": 0, "shipped": 0}
        ]
    await assert_invariants(committed_db, sku)


async def test_backorder_path(committed_db: AsyncEngine) -> None:
    sku = await seed_sku_locations_stock(committed_db, "A", {"L1": 2})
    async with _client(committed_db) as client:
        created = await client.post(
            "/orders",
            json={"lines": [{"sku_id": "A", "qty": 5}]},
            headers={"Idempotency-Key": "c1"},
        )
        assert created.status_code == 201
        order_id = created.json()["order_id"]
        allocated = await client.post(
            f"/orders/{order_id}/allocate", headers={"Idempotency-Key": "a1"}
        )
        assert allocated.status_code == 200
        assert allocated.json()["state"] == "backordered"
        read = await client.get(f"/orders/{order_id}")
        assert read.json()["lines"][0]["allocated"] == 2
    await assert_invariants(committed_db, sku)


async def test_http_allocate_replay_is_one_effect(committed_db: AsyncEngine) -> None:
    sku = await seed_sku_locations_stock(committed_db, "A", {"L1": 5})
    async with _client(committed_db) as client:
        created = await client.post(
            "/orders",
            json={"lines": [{"sku_id": "A", "qty": 5}]},
            headers={"Idempotency-Key": "c1"},
        )
        order_id = created.json()["order_id"]
        first = await client.post(f"/orders/{order_id}/allocate", headers={"Idempotency-Key": "a1"})
        second = await client.post(
            f"/orders/{order_id}/allocate", headers={"Idempotency-Key": "a1"}
        )
    assert first.status_code == 200
    assert second.status_code == 200
    assert first.json() == second.json()
    async with committed_db.connect() as conn:
        rows = (
            await conn.execute(
                select(reservation.c.reservation_id).where(reservation.c.order_id == order_id)
            )
        ).all()
    assert len(rows) == 1
    await assert_invariants(committed_db, sku)
