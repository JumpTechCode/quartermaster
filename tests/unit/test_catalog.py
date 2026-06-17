"""Sku and Location are thin, validated reference records (design spec §3)."""

from __future__ import annotations

import pytest

from quartermaster.domain.catalog import Location, LocationKind, Sku
from quartermaster.domain.ids import LocationId, SkuId


def test_sku_holds_its_fields() -> None:
    sku = Sku(sku_id=SkuId("WIDGET-1"), description="Blue widget", unit="each")
    assert sku.sku_id == "WIDGET-1"
    assert sku.description == "Blue widget"
    assert sku.unit == "each"


def test_location_holds_its_fields() -> None:
    loc = Location(location_id=LocationId("A-01-1"), kind=LocationKind.SHELF)
    assert loc.location_id == "A-01-1"
    assert loc.kind is LocationKind.SHELF


def test_location_kinds_match_the_spec() -> None:
    assert {k.value for k in LocationKind} == {"shelf", "receiving", "staging", "dock"}


@pytest.mark.parametrize("field", ["sku_id", "description", "unit"])
def test_sku_rejects_blank_fields(field: str) -> None:
    kwargs = {"sku_id": SkuId("WIDGET-1"), "description": "Blue widget", "unit": "each"}
    kwargs[field] = ""
    with pytest.raises(ValueError):
        Sku(**kwargs)  # type: ignore[arg-type]


def test_location_rejects_blank_id() -> None:
    with pytest.raises(ValueError):
        Location(location_id=LocationId(""), kind=LocationKind.SHELF)


def test_records_are_immutable() -> None:
    sku = Sku(sku_id=SkuId("WIDGET-1"), description="Blue widget", unit="each")
    with pytest.raises(Exception):  # noqa: B017 - frozen dataclass raises FrozenInstanceError
        sku.unit = "case"  # type: ignore[misc]
