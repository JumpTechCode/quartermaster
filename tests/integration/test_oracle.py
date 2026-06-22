"""The offline invariant oracle against real Postgres (design §7).

Three groups:
 * adapter-shape: the four aggregate reads return correct values;
 * agreement: after a real mixed command sequence touching every movement type,
   the oracle's independent reconstruction matches the live tables (all OK);
 * detection: a single CHECK-legal corruption trips the matching check.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from quartermaster.adapters.postgres.identifiers import (
    new_movement_id,
    new_order_id,
    new_receipt_id,
    new_reservation_id,
)
from quartermaster.adapters.postgres.unit_of_work import (
    postgres_read_uow_factory,
    postgres_uow_factory,
)
from quartermaster.application.clock import system_clock
from quartermaster.application.handlers.allocate import run_allocate
from quartermaster.application.handlers.arrive import run_arrive
from quartermaster.application.handlers.cancel import run_cancel
from quartermaster.application.handlers.create_order import run_create_order
from quartermaster.application.handlers.create_receipt import run_create_receipt
from quartermaster.application.handlers.create_return import run_create_return
from quartermaster.application.handlers.pack import run_pack
from quartermaster.application.handlers.pick import run_pick
from quartermaster.application.handlers.putaway import run_putaway
from quartermaster.application.handlers.receive import run_receive
from quartermaster.application.handlers.ship import run_ship
from quartermaster.application.oracle import CheckStatus, run_oracle
from quartermaster.application.ports import UnitOfWorkFactory
from quartermaster.domain.ids import IdempotencyKey, LocationId, SkuId
from quartermaster.workers.reservation_reaper import reap_reservations
from tests.integration.seed import seed_location, seed_sku

S = SkuId("S")
DOCK = LocationId("DOCK")
A1 = LocationId("A1")


async def _receive_and_putaway(factory: UnitOfWorkFactory, qty: int, *, tag: str) -> None:
    """Drive a supplier receipt for ``qty`` units of S: receive at DOCK, putaway to A1."""
    r = await run_create_receipt(
        factory,
        ((S, qty),),
        IdempotencyKey(f"cr-{tag}"),
        now=system_clock,
        new_receipt_id=new_receipt_id,
    )
    await run_arrive(factory, r.receipt_id, IdempotencyKey(f"ar-{tag}"))
    await run_receive(
        factory,
        r.receipt_id,
        DOCK,
        ((S, qty),),
        IdempotencyKey(f"rc-{tag}"),
        now=system_clock,
        new_movement_id=new_movement_id,
    )
    await run_putaway(
        factory,
        r.receipt_id,
        DOCK,
        A1,
        IdempotencyKey(f"pa-{tag}"),
        now=system_clock,
        new_movement_id=new_movement_id,
    )


async def test_oracle_adapter_reads(committed_db: AsyncEngine) -> None:
    await seed_sku(committed_db, "S")
    await seed_location(committed_db, "DOCK", "receiving")
    await seed_location(committed_db, "A1", "shelf")
    factory = postgres_uow_factory(committed_db)
    await _receive_and_putaway(factory, 10, tag="x")
    async with factory() as uow:
        cells = await uow.stock.all_cells()
        totals = await uow.movements.aggregate()
    by_cell = {(c.sku_id, c.location_id): (c.on_hand, c.reserved) for c in cells}
    assert by_cell[(S, A1)] == (10, 0)
    assert by_cell[(S, DOCK)] == (0, 0)
    # one RECEIVE (->DOCK, 10) and one PUTAWAY (DOCK->A1, 10)
    kinds = {(t.type.value, t.from_location, t.to_location, t.total_qty) for t in totals}
    assert ("receive", None, DOCK, 10) in kinds
    assert ("putaway", DOCK, A1, 10) in kinds


async def test_oracle_agrees_with_real_command_path(committed_db: AsyncEngine) -> None:
    await seed_sku(committed_db, "S")
    await seed_location(committed_db, "DOCK", "receiving")
    await seed_location(committed_db, "A1", "shelf")
    factory = postgres_uow_factory(committed_db)

    # inbound: 10 on the shelf
    await _receive_and_putaway(factory, 10, tag="in")

    # O1: allocate -> pick -> pack -> ship  (RESERVE, PICK)
    o1_result = await run_create_order(
        factory, ((S, 4),), IdempotencyKey("o1"), now=system_clock, new_order_id=new_order_id
    )
    o1 = o1_result.order_id
    await run_allocate(
        factory,
        o1,
        IdempotencyKey("o1-al"),
        now=system_clock,
        new_reservation_id=new_reservation_id,
        new_movement_id=new_movement_id,
    )
    await run_pick(
        factory, o1, IdempotencyKey("o1-pk"), now=system_clock, new_movement_id=new_movement_id
    )
    await run_pack(factory, o1, IdempotencyKey("o1-pc"))
    await run_ship(factory, o1, IdempotencyKey("o1-sh"))

    # O2: allocate -> cancel  (RESERVE, RELEASE)
    o2_result = await run_create_order(
        factory, ((S, 3),), IdempotencyKey("o2"), now=system_clock, new_order_id=new_order_id
    )
    o2 = o2_result.order_id
    await run_allocate(
        factory,
        o2,
        IdempotencyKey("o2-al"),
        now=system_clock,
        new_reservation_id=new_reservation_id,
        new_movement_id=new_movement_id,
    )
    await run_cancel(
        factory, o2, IdempotencyKey("o2-cx"), now=system_clock, new_movement_id=new_movement_id
    )

    # O3: allocate -> reaper expire  (RESERVE, EXPIRE)
    o3_result = await run_create_order(
        factory, ((S, 2),), IdempotencyKey("o3"), now=system_clock, new_order_id=new_order_id
    )
    o3 = o3_result.order_id
    await run_allocate(
        factory,
        o3,
        IdempotencyKey("o3-al"),
        now=system_clock,
        new_reservation_id=new_reservation_id,
        new_movement_id=new_movement_id,
    )
    # run the reaper with a clock far enough ahead that the 15-min TTL has passed
    future = lambda: datetime.now(UTC) + timedelta(hours=1)  # noqa: E731
    run = await reap_reservations(
        factory, now=future, new_movement_id=new_movement_id, batch_size=10
    )
    assert run.acted == 1

    # RMA: return 1 of the shipped units, back onto the shelf  (RECEIVE, PUTAWAY)
    ret = await run_create_return(
        factory,
        o1,
        ((S, 1),),
        IdempotencyKey("ret"),
        now=system_clock,
        new_receipt_id=new_receipt_id,
    )
    await run_arrive(factory, ret.receipt_id, IdempotencyKey("ret-ar"))
    await run_receive(
        factory,
        ret.receipt_id,
        DOCK,
        ((S, 1),),
        IdempotencyKey("ret-rc"),
        now=system_clock,
        new_movement_id=new_movement_id,
    )
    await run_putaway(
        factory,
        ret.receipt_id,
        DOCK,
        A1,
        IdempotencyKey("ret-pa"),
        now=system_clock,
        new_movement_id=new_movement_id,
    )

    report = await run_oracle(postgres_read_uow_factory(committed_db))
    assert report.ok, [
        (c.name, c.status, c.discrepancies) for c in report.checks if c.status is CheckStatus.FAILED
    ]


async def test_oracle_detects_on_hand_drift(committed_db: AsyncEngine) -> None:
    await seed_sku(committed_db, "S")
    await seed_location(committed_db, "DOCK", "receiving")
    await seed_location(committed_db, "A1", "shelf")
    factory = postgres_uow_factory(committed_db)
    await _receive_and_putaway(factory, 5, tag="d")
    # CHECK-legal corruption: inflate on-hand on the shelf cell
    async with committed_db.begin() as conn:
        await conn.execute(
            text(
                "UPDATE stock SET qty_on_hand = qty_on_hand + 1"
                " WHERE sku_id='S' AND location_id='A1'"
            )
        )
    report = await run_oracle(postgres_read_uow_factory(committed_db))
    check = report.check("conservation_on_hand")
    assert check.status is CheckStatus.FAILED
    d = next(d for d in check.discrepancies if d.location_id == A1)
    assert (d.expected, d.actual) == (5, 6)


async def test_oracle_detects_oversell(committed_db: AsyncEngine) -> None:
    await seed_sku(committed_db, "S")
    await seed_location(committed_db, "DOCK", "receiving")
    await seed_location(committed_db, "A1", "shelf")
    factory = postgres_uow_factory(committed_db)
    await _receive_and_putaway(factory, 5, tag="o")
    o_result = await run_create_order(
        factory, ((S, 5),), IdempotencyKey("o"), now=system_clock, new_order_id=new_order_id
    )
    o = o_result.order_id
    await run_allocate(
        factory,
        o,
        IdempotencyKey("o-al"),
        now=system_clock,
        new_reservation_id=new_reservation_id,
        new_movement_id=new_movement_id,
    )
    await run_pick(
        factory, o, IdempotencyKey("o-pk"), now=system_clock, new_movement_id=new_movement_id
    )
    await run_pack(factory, o, IdempotencyKey("o-pc"))
    await run_ship(factory, o, IdempotencyKey("o-sh"))
    # delete the RECEIVE movement: ever_received drops below shipped+on_hand
    async with committed_db.begin() as conn:
        await conn.execute(text("DELETE FROM movement WHERE type='receive'"))
    report = await run_oracle(postgres_read_uow_factory(committed_db))
    assert report.check("no_oversell").status is CheckStatus.FAILED


async def test_oracle_detects_reserved_drift(committed_db: AsyncEngine) -> None:
    await seed_sku(committed_db, "S")
    await seed_location(committed_db, "DOCK", "receiving")
    await seed_location(committed_db, "A1", "shelf")
    factory = postgres_uow_factory(committed_db)
    await _receive_and_putaway(factory, 5, tag="r")
    o_result = await run_create_order(
        factory, ((S, 3),), IdempotencyKey("o"), now=system_clock, new_order_id=new_order_id
    )
    o = o_result.order_id
    await run_allocate(
        factory,
        o,
        IdempotencyKey("o-al"),
        now=system_clock,
        new_reservation_id=new_reservation_id,
        new_movement_id=new_movement_id,
    )
    # inject an extra RESERVE movement: reserved_ledger now exceeds the stock row
    async with committed_db.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO movement (movement_id, ts, type, sku_id, from_location, to_location,"
                " qty, ref, command_id) VALUES (gen_random_uuid(), now(), 'reserve', 'S', NULL,"
                " 'A1', 1, gen_random_uuid(), 'corrupt')"
            )
        )
    report = await run_oracle(postgres_read_uow_factory(committed_db))
    assert report.check("conservation_reserved").status is CheckStatus.FAILED
