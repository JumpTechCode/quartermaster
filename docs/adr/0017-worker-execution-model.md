# 0017 — Polled worker execution model

- Status: Accepted
- Date: 2026-06-20

## Context

The background workers (0010) need a runtime shape. Section 5.5 fixes the
*properties* — polled, idempotent, bounded per-item transactions — but not how a
worker is structured, tested, or launched. Two forces constrain the choice: the
import-linter contract forbids `workers` from importing `adapters` (transitively,
so `workers.__main__ → app → adapters` is also banned), and the load harness and
tests need to drive a worker deterministically rather than wait on a wall-clock
loop.

## Decision

Each worker is a **bounded pass** — `reap_*(uow_factory, *, ...) -> ReaperRun` —
that does one unit of work against injected seams and returns telemetry. A
generic `run_forever(tick, *, interval, sleep, stop)` driver schedules a pass on
a fixed interval; a tick that raises is logged and the loop continues, so a
transient database error never kills the worker. `sleep` and `stop` are seams, so
tests run an exact number of iterations with no real delay, and the pass function
is what the load harness calls directly.

Composition and the process entrypoint live in `app.py`, the sole composition
root: `run_workers()` builds the engine and seams, installs SIGTERM/SIGINT
handlers that set a shared stop event, and runs both loops under
`asyncio.gather`. `python -m quartermaster.app` runs the worker process,
mirroring how the API is served via `quartermaster.app:build_app`. `workers/`
itself imports only `application` and `domain`.

## Consequences

- The entrypoint cannot live in `workers/__main__.py` without violating the
  transitive `workers → adapters` ban; keeping it in `app.py` preserves the
  single-composition-root invariant.
- Reaper passes are unit-testable with fakes and integration-testable against
  real Postgres, and are reusable verbatim by the load harness.
- The same `run_forever` driver serves the backorder fulfilment sweep (the next
  worker slice) unchanged.
- A worker that needs its own scaling profile later can be split into a separate
  process by adding another entrypoint in `app.py`; the pass functions do not
  change.
