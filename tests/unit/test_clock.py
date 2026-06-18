"""Unit tests for the system clock."""

from __future__ import annotations

from datetime import UTC

from quartermaster.application.clock import system_clock


def test_system_clock_is_timezone_aware_utc() -> None:
    now = system_clock()
    assert now.tzinfo is not None
    assert now.utcoffset() == UTC.utcoffset(None)
