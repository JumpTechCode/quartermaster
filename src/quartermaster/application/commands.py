"""Command value types and their idempotency fingerprints.

The fingerprint is a stable hash of the command's *semantic* content (not its
idempotency key). The same key presented with a different fingerprint is a
key-reuse error; two different keys for the same command share a fingerprint.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any

from quartermaster.domain.ids import IdempotencyKey, OrderId, SkuId


def _fingerprint(payload: dict[str, Any]) -> str:
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class AllocateCommand:
    """Reserve available stock for every outstanding line of an order."""

    order_id: OrderId
    key: IdempotencyKey

    def fingerprint(self) -> str:
        return _fingerprint({"command": "allocate", "order_id": str(self.order_id)})


@dataclass(frozen=True)
class CreateOrderCommand:
    """Create a new order with the given lines in the ``created`` state."""

    lines: tuple[tuple[SkuId, int], ...]
    key: IdempotencyKey

    def fingerprint(self) -> str:
        sorted_lines: list[list[SkuId | int]] = sorted(
            ([sku, qty] for sku, qty in self.lines),
            key=lambda pair: pair[0],
        )
        return _fingerprint({"command": "create_order", "lines": sorted_lines})


@dataclass(frozen=True)
class PickCommand:
    """Consume an allocated order's reservations and advance it to ``picked``."""

    order_id: OrderId
    key: IdempotencyKey

    def fingerprint(self) -> str:
        return _fingerprint({"command": "pick", "order_id": str(self.order_id)})


@dataclass(frozen=True)
class PackCommand:
    """Advance a picked order to ``packed``."""

    order_id: OrderId
    key: IdempotencyKey

    def fingerprint(self) -> str:
        return _fingerprint({"command": "pack", "order_id": str(self.order_id)})


@dataclass(frozen=True)
class ShipCommand:
    """Advance a packed order to ``shipped``, finalizing shipped quantities."""

    order_id: OrderId
    key: IdempotencyKey

    def fingerprint(self) -> str:
        return _fingerprint({"command": "ship", "order_id": str(self.order_id)})


@dataclass(frozen=True)
class CancelCommand:
    """Cancel a pre-pick order, releasing its held reservations."""

    order_id: OrderId
    key: IdempotencyKey

    def fingerprint(self) -> str:
        return _fingerprint({"command": "cancel", "order_id": str(self.order_id)})
