# tests/unit/test_tables.py
"""Structural tests for the Core schema metadata (pure, no database)."""

from __future__ import annotations

from sqlalchemy.dialects import postgresql
from sqlalchemy.schema import CreateTable

from quartermaster.adapters.postgres.tables import metadata

EXPECTED_TABLES = {
    "sku",
    "location",
    "stock",
    "orders",
    "order_line",
    "receipt",
    "receipt_line",
    "reservation",
    "movement",
    "idempotency_key",
}


def _ddl(table_name: str) -> str:
    return str(
        CreateTable(metadata.tables[table_name]).compile(
            dialect=postgresql.dialect()  # type: ignore[no-untyped-call]
        )
    )


def test_metadata_has_exactly_the_expected_tables() -> None:
    assert set(metadata.tables) == EXPECTED_TABLES


def test_order_table_is_pluralized_to_avoid_the_reserved_word() -> None:
    assert "orders" in metadata.tables
    assert "order" not in metadata.tables


def test_stock_primary_key_is_composite() -> None:
    pk = metadata.tables["stock"].primary_key
    assert [c.name for c in pk.columns] == ["sku_id", "location_id"]


def test_stock_check_constraint_names_render() -> None:
    ddl = _ddl("stock")
    assert "ck_stock_on_hand_nonneg" in ddl
    assert "ck_stock_reserved_nonneg" in ddl
    assert "ck_stock_reserved_le_on_hand" in ddl


def test_receipt_has_the_rma_origin_cross_field_check() -> None:
    assert "ck_receipt_rma_origin" in _ddl("receipt")


def test_movement_ref_and_command_id_have_no_foreign_keys() -> None:
    movement = metadata.tables["movement"]
    assert movement.c.ref.foreign_keys == set()
    assert movement.c.command_id.foreign_keys == set()


def test_movement_has_the_sku_ts_index() -> None:
    movement = metadata.tables["movement"]
    indexed = {tuple(col.name for col in ix.columns) for ix in movement.indexes}
    assert ("sku_id", "ts") in indexed


def test_enum_checks_are_sourced_from_the_domain() -> None:
    # The order-state CHECK must list every domain OrderState value.
    from quartermaster.domain.state_machines import OrderState

    ddl = _ddl("orders")
    for state in OrderState:
        assert f"'{state.value}'" in ddl
