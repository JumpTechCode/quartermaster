"""Read transactions hold one MVCC snapshot across statements (issue #70).

A read serves a multi-statement view (e.g. order header + lines) inside one
transaction. Under READ COMMITTED each statement takes a fresh snapshot, so a
command committing between two statements is partially visible -- a header/lines
pair (and ``version``) that never atomically coexisted. The read UoW pins
REPEATABLE READ so every statement shares the snapshot taken at the first read.

The interleaving here is explicit (not a race): the read UoW issues its first
statement, then a separate transaction commits a version bump, then the read UoW
reads again. The second read must still observe the pre-commit snapshot.
"""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy.ext.asyncio import AsyncEngine

from quartermaster.adapters.postgres.identifiers import new_movement_id
from quartermaster.adapters.postgres.tables import movement, stock
from quartermaster.adapters.postgres.unit_of_work import (
    postgres_read_uow_factory,
    postgres_uow_factory,
)
from quartermaster.domain.movements import MovementType
from quartermaster.domain.state_machines import OrderState
from tests.integration.seed import seed_order, seed_sku, seed_sku_locations_stock


async def test_read_uow_holds_one_snapshot_across_statements(
    committed_db: AsyncEngine,
) -> None:
    await seed_sku(committed_db, "A")
    order_id = await seed_order(committed_db, state=OrderState.CREATED, lines={"A": 5})
    read_factory = postgres_read_uow_factory(committed_db)
    write_factory = postgres_uow_factory(committed_db)

    async with read_factory() as read_uow:
        before = await read_uow.orders.get(order_id)
        assert before is not None and before.version == 1

        # A concurrent command commits a version bump between the two reads.
        async with write_factory() as writer:
            assert await writer.orders.cas_state(
                order_id, OrderState.CREATED, 1, OrderState.ALLOCATED
            )
            await writer.commit()

        # Same transaction, second statement: still the snapshot from `before`.
        after = await read_uow.orders.get(order_id)
        assert after is not None
        assert after.version == 1
        assert after.state is OrderState.CREATED

    # The write really committed: a fresh snapshot observes the bump.
    async with read_factory() as fresh:
        latest = await fresh.orders.get(order_id)
        assert latest is not None
        assert latest.version == 2
        assert latest.state is OrderState.ALLOCATED


async def test_write_uow_takes_a_fresh_snapshot_per_statement(
    committed_db: AsyncEngine,
) -> None:
    """Contrast: the READ COMMITTED command UoW sees the mid-transaction commit.

    This is exactly the torn read the command path tolerates (its conditional
    ``WHERE`` is the guard, not a prior read) but the read path must not expose.
    """
    await seed_sku(committed_db, "A")
    order_id = await seed_order(committed_db, state=OrderState.CREATED, lines={"A": 5})
    write_factory = postgres_uow_factory(committed_db)

    async with write_factory() as reader:
        before = await reader.orders.get(order_id)
        assert before is not None and before.version == 1

        async with write_factory() as writer:
            assert await writer.orders.cas_state(
                order_id, OrderState.CREATED, 1, OrderState.ALLOCATED
            )
            await writer.commit()

        after = await reader.orders.get(order_id)
        assert after is not None
        assert after.version == 2


async def test_oracle_read_uow_cross_checks_one_snapshot(
    committed_db: AsyncEngine,
) -> None:
    """The oracle's ledger read and stock read fold over a single instant (issue #70).

    ``run_oracle`` issues four base-table reads in one UoW. If a command commits
    between the ledger aggregate and the stock read, a READ COMMITTED oracle would
    compare totals taken at two instants -- a torn cross-check that can FAIL on
    consistent data. The read UoW pins REPEATABLE READ so both reads see the
    snapshot taken at the first read.
    """
    await seed_sku_locations_stock(committed_db, "A", {"S1": 10})
    async with committed_db.begin() as conn:
        await conn.execute(
            movement.insert().values(
                movement_id=new_movement_id(),
                ts=datetime(2026, 6, 20, tzinfo=UTC),
                type=MovementType.RECEIVE.value,
                sku_id="A",
                from_location=None,
                to_location="S1",
                qty=10,
                ref=new_movement_id(),
                command_id="seed",
            )
        )

    read_factory = postgres_read_uow_factory(committed_db)
    async with read_factory() as oracle_uow:
        cells_before = await oracle_uow.stock.all_cells()
        assert {(c.location_id, c.on_hand) for c in cells_before} == {("S1", 10)}

        # A RECEIVE commits between the ledger read and the stock read.
        async with committed_db.begin() as conn:
            await conn.execute(
                movement.insert().values(
                    movement_id=new_movement_id(),
                    ts=datetime(2026, 6, 20, tzinfo=UTC),
                    type=MovementType.RECEIVE.value,
                    sku_id="A",
                    from_location=None,
                    to_location="S1",
                    qty=7,
                    ref=new_movement_id(),
                    command_id="concurrent",
                )
            )
            await conn.execute(
                stock.update()
                .where(stock.c.sku_id == "A", stock.c.location_id == "S1")
                .values(qty_on_hand=17)
            )

        totals = await oracle_uow.movements.aggregate()
        cells_after = await oracle_uow.stock.all_cells()

        # Both reads reflect the snapshot: ledger total 10 matches on_hand 10.
        assert sum(t.total_qty for t in totals) == 10
        assert {(c.location_id, c.on_hand) for c in cells_after} == {("S1", 10)}
