"""Correctness-under-load harness (design spec §7; this slice's spec at
docs/superpowers/specs/2026-06-22-load-harness-design.md).

A standalone driver — not part of the ``quartermaster`` engine. It imports the
application and adapters freely (it is a consumer, like ``api``/``workers``), and
is held to mypy --strict but is exercised, not coverage-gated, by the engine's
suite.
"""
