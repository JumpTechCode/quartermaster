"""The clock seam: time enters the engine through an injected callable.

Handlers stamp reservation expiries and ledger timestamps via a ``Clock`` so
tests can pin time deterministically; the composition root injects
``system_clock``. Keeping time behind a seam also lets the future reservation
reaper share one definition of "now".
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime

Clock = Callable[[], datetime]


def system_clock() -> datetime:
    """Return the current instant as a timezone-aware UTC ``datetime``."""
    return datetime.now(UTC)
