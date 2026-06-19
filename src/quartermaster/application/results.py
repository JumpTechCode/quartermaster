"""Command result types and their JSON encoding for the idempotency response.

Results are stored in the ``idempotency_key.response`` JSONB column and decoded
verbatim on replay, so encoding is explicit and JSON-safe (UUIDs and enums as
strings).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from uuid import UUID

from quartermaster.domain.ids import OrderId, ReservationId, SkuId
from quartermaster.domain.state_machines import OrderState


@dataclass(frozen=True)
class LineAllocation:
    """How much of one line was allocated by the command."""

    sku_id: SkuId
    allocated: int


@dataclass(frozen=True)
class AllocateResult:
    """The outcome of an ``allocate``: resulting state and what was reserved."""

    order_id: OrderId
    state: OrderState
    lines: tuple[LineAllocation, ...]
    reservation_ids: tuple[ReservationId, ...]

    def to_response(self) -> dict[str, Any]:
        return {
            "order_id": str(self.order_id),
            "state": self.state.value,
            "lines": [{"sku_id": line.sku_id, "allocated": line.allocated} for line in self.lines],
            "reservation_ids": [str(rid) for rid in self.reservation_ids],
        }

    @classmethod
    def decode(cls, data: dict[str, Any]) -> AllocateResult:
        return cls(
            order_id=OrderId(UUID(data["order_id"])),
            state=OrderState(data["state"]),
            lines=tuple(
                LineAllocation(SkuId(line["sku_id"]), int(line["allocated"]))
                for line in data["lines"]
            ),
            reservation_ids=tuple(ReservationId(UUID(rid)) for rid in data["reservation_ids"]),
        )


@dataclass(frozen=True)
class CreatedLine:
    """One line of a newly created order: its ordered quantity."""

    sku_id: SkuId
    ordered: int


@dataclass(frozen=True)
class CreateOrderResult:
    """The outcome of a ``create_order``: the new order id, state, and lines."""

    order_id: OrderId
    state: OrderState
    lines: tuple[CreatedLine, ...]

    def to_response(self) -> dict[str, Any]:
        return {
            "order_id": str(self.order_id),
            "state": self.state.value,
            "lines": [{"sku_id": line.sku_id, "ordered": line.ordered} for line in self.lines],
        }

    @classmethod
    def decode(cls, data: dict[str, Any]) -> CreateOrderResult:
        return cls(
            order_id=OrderId(UUID(data["order_id"])),
            state=OrderState(data["state"]),
            lines=tuple(
                CreatedLine(SkuId(line["sku_id"]), int(line["ordered"])) for line in data["lines"]
            ),
        )
