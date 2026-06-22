"""Seeded workload construction for the harness: bulk seeding + command thunks.

The *workload* is deterministic in the seed (which SKU each order wants); the
*interleaving* is not (design spec §6). The allocate thunk drives the envelope
directly so the harness can inject the counting ``sleep`` (retry instrumentation)
and a seeded ``rand`` (reproducible jitter) that the ``run_allocate`` convenience
wrapper does not forward (design spec §5).
"""

from __future__ import annotations

import random
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from loadtest.runner import CommandThunk, Rand, Sleep
from quartermaster.adapters.postgres.identifiers import (
    new_movement_id,
    new_order_id,
    new_receipt_id,
    new_reservation_id,
)
from quartermaster.adapters.postgres.tables import (
    location,
    movement,
    order_line,
    orders,
    sku,
    stock,
)
from quartermaster.application.clock import system_clock
from quartermaster.application.commands import AllocateCommand
from quartermaster.application.envelope import execute
from quartermaster.application.handlers.allocate import allocate, run_allocate
from quartermaster.application.handlers.arrive import run_arrive
from quartermaster.application.handlers.close_receipt import run_close_receipt
from quartermaster.application.handlers.create_order import run_create_order
from quartermaster.application.handlers.create_receipt import run_create_receipt
from quartermaster.application.handlers.create_return import run_create_return
from quartermaster.application.handlers.pack import run_pack
from quartermaster.application.handlers.pick import run_pick
from quartermaster.application.handlers.putaway import run_putaway
from quartermaster.application.handlers.receive import run_receive
from quartermaster.application.handlers.ship import run_ship
from quartermaster.application.ports import UnitOfWork, UnitOfWorkFactory
from quartermaster.application.results import AllocateResult
from quartermaster.domain.ids import IdempotencyKey, LocationId, OrderId, SkuId
from quartermaster.domain.state_machines import OrderState

# Children before parents: a TRUNCATE ... CASCADE order that mirrors the
# integration conftest's _ALL_TABLES. Harness-local so loadtest never imports tests.
_ALL_TABLES: tuple[str, ...] = (
    "movement",
    "reservation",
    "order_line",
    "orders",
    "receipt_line",
    "receipt",
    "stock",
    "idempotency_key",
    "sku",
    "location",
)


async def truncate_all(engine: AsyncEngine) -> None:
    """Reset every table so each strategy run starts from an empty store."""
    async with engine.begin() as conn:
        await conn.execute(text(f"TRUNCATE {', '.join(_ALL_TABLES)} RESTART IDENTITY CASCADE"))


@dataclass(frozen=True)
class ComparativeSeed:
    """The hot SKUs and the CREATED orders contending for them."""

    sku_ids: tuple[SkuId, ...]
    order_ids: tuple[OrderId, ...]


async def seed_comparative(
    engine: AsyncEngine,
    *,
    n_skus: int,
    n_orders: int,
    on_hand_per_cell: int,
    qty_per_order: int,
    rng: random.Random,
) -> ComparativeSeed:
    """Seed ``n_skus`` hot cells with scarce on-hand and ``n_orders`` CREATED orders.

    Each order wants ``qty_per_order`` of one randomly-chosen hot SKU. Scarce
    ``on_hand_per_cell`` relative to demand concentrates contention on the cells.
    """
    sku_ids = tuple(SkuId(f"HOT-{i}") for i in range(n_skus))
    loc_ids = tuple(f"S{i}" for i in range(n_skus))
    order_ids: list[OrderId] = []
    async with engine.begin() as conn:
        for s in sku_ids:
            await conn.execute(sku.insert().values(sku_id=s, description="hot", unit="each"))
        for loc in loc_ids:
            await conn.execute(location.insert().values(location_id=loc, kind="shelf"))
        for s, loc in zip(sku_ids, loc_ids, strict=True):
            await conn.execute(
                stock.insert().values(
                    sku_id=s, location_id=loc, qty_on_hand=on_hand_per_cell, qty_reserved=0
                )
            )
            # Synthetic RECEIVE movement so the oracle's on-hand ledger reconstruction
            # agrees with the seeded stock row. Without this, conservation_on_hand
            # always fails (ledger sees 0; stock table sees on_hand_per_cell).
            await conn.execute(
                movement.insert().values(
                    movement_id=new_movement_id(),
                    ts=datetime.now(UTC),
                    type="receive",
                    sku_id=s,
                    from_location=None,
                    to_location=loc,
                    qty=on_hand_per_cell,
                    ref=new_movement_id(),  # synthetic ref UUID; no FK on movement.ref
                    command_id=f"seed-receive-{s}-{loc}",
                )
            )
        for _ in range(n_orders):
            oid = new_order_id()
            chosen = rng.choice(sku_ids)
            await conn.execute(
                orders.insert().values(
                    order_id=oid,
                    state=OrderState.CREATED.value,
                    version=1,
                    created_at=datetime.now(UTC),
                )
            )
            await conn.execute(
                order_line.insert().values(
                    order_id=oid,
                    sku_id=chosen,
                    ordered_qty=qty_per_order,
                    allocated_qty=0,
                    picked_qty=0,
                    shipped_qty=0,
                )
            )
            order_ids.append(oid)
    return ComparativeSeed(sku_ids=sku_ids, order_ids=tuple(order_ids))


def allocate_thunk(
    uow_factory: UnitOfWorkFactory, order_id: OrderId, key: IdempotencyKey
) -> CommandThunk:
    """An allocate command bound to the envelope, accepting injected sleep/rand."""

    async def thunk(sleep: Sleep, rand: Rand) -> AllocateResult:
        command = AllocateCommand(order_id, key)

        async def handler(uow: UnitOfWork, cmd: AllocateCommand) -> AllocateResult:
            return await allocate(
                uow,
                cmd,
                now=system_clock,
                new_reservation_id=new_reservation_id,
                new_movement_id=new_movement_id,
            )

        return await execute(
            uow_factory, command, handler, AllocateResult.decode, sleep=sleep, rand=rand
        )

    return thunk


@dataclass(frozen=True)
class ChainSpec:
    """One independent document chain: a SKU and its receiving + shelf cells."""

    sku_id: SkuId
    receiving: LocationId
    shelf: LocationId
    qty: int


@dataclass(frozen=True)
class MixedSeed:
    chains: tuple[ChainSpec, ...]


async def seed_mixed(
    engine: AsyncEngine, *, n_chains: int, qty: int, rng: random.Random
) -> MixedSeed:
    """Seed ``n_chains`` independent (SKU, receiving cell, shelf cell) triples.

    Independent chains exercise every command path under concurrent load without
    cross-chain contention, so a correct engine must leave the oracle green; the
    comparative run (Task 6) is where contention is concentrated. ``rng`` is taken
    for signature parity with seed_comparative and future shared-SKU variants.
    """
    chains: list[ChainSpec] = []
    async with engine.begin() as conn:
        for i in range(n_chains):
            s = SkuId(f"MIX-{i}")
            recv = LocationId(f"R{i}")
            shelf = LocationId(f"H{i}")
            await conn.execute(sku.insert().values(sku_id=s, description="mix", unit="each"))
            await conn.execute(location.insert().values(location_id=recv, kind="receiving"))
            await conn.execute(location.insert().values(location_id=shelf, kind="shelf"))
            chains.append(ChainSpec(sku_id=s, receiving=recv, shelf=shelf, qty=qty))
    return MixedSeed(chains=tuple(chains))


def chain_thunk(
    uow_factory: UnitOfWorkFactory,
    chain: ChainSpec,
    *,
    idx: int,
    dup_step: bool,
    do_return: bool,
) -> CommandThunk:
    """Drive one chain end to end: inbound receipt → putaway → outbound → ship.

    ``dup_step`` re-fires the allocate with the same key (duplicate injection;
    exactly-once must absorb it). ``do_return`` appends an RMA tail that re-enters
    the shipped units (create_return → arrive → receive → putaway → close).

    Receipt lifecycle order is load-bearing: the state machine is
    EXPECTED → ARRIVED → RECEIVING → RECEIVED → PUTAWAY_COMPLETE → CLOSED, so
    putaway (received → putaway_complete) MUST precede close (putaway_complete →
    closed); reversing them raises IllegalTransition and aborts the chain.
    """

    async def thunk(sleep: Sleep, rand: Rand) -> None:
        s, recv, shelf, q = chain.sku_id, chain.receiving, chain.shelf, chain.qty
        kp = f"mix-{idx}"
        receipt = await run_create_receipt(
            uow_factory,
            ((s, q),),
            IdempotencyKey(f"{kp}-cr"),
            now=system_clock,
            new_receipt_id=new_receipt_id,
        )
        rid = receipt.receipt_id
        await run_arrive(uow_factory, rid, IdempotencyKey(f"{kp}-ar"))
        await run_receive(
            uow_factory,
            rid,
            recv,
            ((s, q),),
            IdempotencyKey(f"{kp}-rc"),
            now=system_clock,
            new_movement_id=new_movement_id,
        )
        await run_putaway(
            uow_factory,
            rid,
            recv,
            shelf,
            IdempotencyKey(f"{kp}-pa"),
            now=system_clock,
            new_movement_id=new_movement_id,
        )
        await run_close_receipt(uow_factory, rid, IdempotencyKey(f"{kp}-cl"))
        order = await run_create_order(
            uow_factory,
            ((s, q),),
            IdempotencyKey(f"{kp}-co"),
            now=system_clock,
            new_order_id=new_order_id,
        )
        oid = order.order_id
        allocate_key = IdempotencyKey(f"{kp}-al")
        await run_allocate(
            uow_factory,
            oid,
            allocate_key,
            now=system_clock,
            new_reservation_id=new_reservation_id,
            new_movement_id=new_movement_id,
        )
        if dup_step:
            await run_allocate(
                uow_factory,
                oid,
                allocate_key,
                now=system_clock,
                new_reservation_id=new_reservation_id,
                new_movement_id=new_movement_id,
            )
        await run_pick(
            uow_factory,
            oid,
            IdempotencyKey(f"{kp}-pk"),
            now=system_clock,
            new_movement_id=new_movement_id,
        )
        await run_pack(uow_factory, oid, IdempotencyKey(f"{kp}-pck"))
        await run_ship(uow_factory, oid, IdempotencyKey(f"{kp}-sh"))
        if do_return:
            rma = await run_create_return(
                uow_factory,
                oid,
                ((s, q),),
                IdempotencyKey(f"{kp}-rt"),
                now=system_clock,
                new_receipt_id=new_receipt_id,
            )
            rrid = rma.receipt_id
            await run_arrive(uow_factory, rrid, IdempotencyKey(f"{kp}-rt-ar"))
            await run_receive(
                uow_factory,
                rrid,
                recv,
                ((s, q),),
                IdempotencyKey(f"{kp}-rt-rc"),
                now=system_clock,
                new_movement_id=new_movement_id,
            )
            await run_putaway(
                uow_factory,
                rrid,
                recv,
                shelf,
                IdempotencyKey(f"{kp}-rt-pa"),
                now=system_clock,
                new_movement_id=new_movement_id,
            )
            await run_close_receipt(uow_factory, rrid, IdempotencyKey(f"{kp}-rt-cl"))

    return thunk
