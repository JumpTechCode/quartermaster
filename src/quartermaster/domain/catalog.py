"""Reference (master) data: SKUs and storage locations.

These are thin immutable records — typed, validated values with no behaviour. A
SKU is identified by its natural code; a location by its natural code plus the
kind of place it is (which allocation and putaway reason about). Blank required
strings are programmer errors, rejected at construction.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from quartermaster.domain.ids import LocationId, SkuId


class LocationKind(StrEnum):
    """The kind of place a location is (design spec §3)."""

    SHELF = "shelf"
    RECEIVING = "receiving"
    STAGING = "staging"
    DOCK = "dock"


def _require_non_empty(value: str, field: str) -> None:
    if not value:
        raise ValueError(f"{field} must be a non-empty string")


@dataclass(frozen=True)
class Sku:
    """A stock-keeping unit, identified by its natural code."""

    sku_id: SkuId
    description: str
    unit: str  # free-form for V1 ("each", "case", ...)

    def __post_init__(self) -> None:
        _require_non_empty(self.sku_id, "sku_id")
        _require_non_empty(self.description, "description")
        _require_non_empty(self.unit, "unit")


@dataclass(frozen=True)
class Location:
    """A storage location, identified by its natural code."""

    location_id: LocationId
    kind: LocationKind

    def __post_init__(self) -> None:
        _require_non_empty(self.location_id, "location_id")
