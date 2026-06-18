"""Command value types and their idempotency fingerprints.

The fingerprint is a stable hash of the command's *semantic* content (not its
idempotency key). The same key presented with a different fingerprint is a
key-reuse error; two different keys for the same command share a fingerprint.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass

from quartermaster.domain.ids import IdempotencyKey, OrderId


def _fingerprint(payload: dict[str, str]) -> str:
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class AllocateCommand:
    """Reserve available stock for every outstanding line of an order."""

    order_id: OrderId
    key: IdempotencyKey

    def fingerprint(self) -> str:
        return _fingerprint({"command": "allocate", "order_id": str(self.order_id)})
