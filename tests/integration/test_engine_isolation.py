"""The engine pins READ COMMITTED regardless of the server's default (issue #71).

Every concurrency guard in the design is reasoned under READ COMMITTED -- the
conditional ``WHERE`` as the guard, the ``FOR UPDATE`` EvalPlanQual re-read, the
``ON CONFLICT DO UPDATE`` re-read. That reasoning holds only at READ COMMITTED.
An operator who sets ``default_transaction_isolation = repeatable read`` (or
serializable) cluster- or role-side must not silently shift those semantics, so
the engine must force the level rather than inherit the server default.
"""

from __future__ import annotations

from sqlalchemy import make_url, text

from quartermaster.adapters.postgres.engine import create_engine


async def test_engine_pins_read_committed_over_a_stricter_server_default(
    postgres_url: str,
) -> None:
    dbname = make_url(postgres_url).database
    assert dbname is not None
    admin = create_engine(postgres_url)
    try:
        # Simulate an operator who set a stricter cluster/role default. ``ALTER
        # DATABASE ... SET`` affects sessions opened after it commits, so the
        # fresh engine below connects under this adverse default.
        async with admin.begin() as conn:
            await conn.execute(
                text(
                    f'ALTER DATABASE "{dbname}" SET default_transaction_isolation = '
                    "'repeatable read'"
                )
            )

        engine = create_engine(postgres_url)
        try:
            async with engine.connect() as conn:
                level = await conn.scalar(text("SELECT current_setting('transaction_isolation')"))
            assert level == "read committed"
        finally:
            await engine.dispose()
    finally:
        async with admin.begin() as conn:
            await conn.execute(
                text(f'ALTER DATABASE "{dbname}" RESET default_transaction_isolation')
            )
        await admin.dispose()
