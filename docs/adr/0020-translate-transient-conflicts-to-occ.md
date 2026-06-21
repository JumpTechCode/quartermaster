# 20. Translate transient Postgres conflicts to OccConflict at the adapter boundary

Date: 2026-06-21

## Status

Accepted

## Context

ADR-0019 introduced opposed lock orderings: `pick`/`cancel` lock the order
header → reservation → line, while the reservation reaper locks
reservation → line → header. A command racing the reaper on the same order can
form an ABBA cycle that Postgres breaks by aborting one transaction with
`DeadlockDetected` (SQLSTATE `40P01`). This is a liveness / error-surface
concern, not a correctness one — the reservation-state CAS still guarantees
exactly-once disposition and every invariant holds whichever side is aborted
(ADR-0003, ADR-0019).

The transaction envelope (ADR-0003) already retries `OccConflict` — the
application-level signal that a document CAS matched no row — with a bounded
budget. A server-broken deadlock means the same thing operationally: nothing
committed, run the transaction again. But it surfaces as a SQLAlchemy/asyncpg
`DBAPIError`, which is none of the envelope's classified types, so it escaped the
`except OccConflict`, rolled the transaction back without finalizing the
idempotency key, and reached the HTTP catch-all as an opaque `500`. The reaper
absorbs its own abort (caught, counted, retried next pass); only the command
path leaked the raw error.

The envelope is deliberately database-agnostic — it runs over in-memory fakes in
the unit suite and must not import the SQLAlchemy/asyncpg error types — so it
cannot itself catch a driver error. The translation has to live in the adapter.

## Decision

Translate the Postgres transient-conflict SQLSTATEs into `OccConflict` in the
async engine factory (`adapters/postgres/engine.py`), via a SQLAlchemy
`handle_error` listener registered on the engine:

- `40P01` deadlock_detected
- `40001` serialization_failure

The listener reads the SQLSTATE off the driver exception (asyncpg exposes
`sqlstate`; the asyncpg dialect re-exposes it on the wrapped DBAPI error) and
re-raises `OccConflict` for those two codes; every other error propagates
unchanged. Because the listener is on the engine, it covers every statement the
repositories execute, so any handler whose statements deadlock raises
`OccConflict` and the envelope's existing bounded retry absorbs it — the loser
re-runs on a fresh connection and typically succeeds, or exhausts the budget and
returns the pipeline's `503`, never a `500`.

A deadlock is not a disconnect, so the connection is not invalidated; the aborted
transaction still rolls back cleanly, which is exactly the `uow.rollback()` the
envelope issues before each retry.

## Consequences

- A `pick`/`cancel` deadlocked against the reaper now resolves as a bounded OCC
  retry rather than an opaque `500`, closing the open item recorded in
  ADR-0019's consequences.
- The translation lives only in the adapter; the application layer keeps no
  knowledge of SQLAlchemy/asyncpg, preserving the fakes-based unit suite and the
  import boundaries (ADR-0012).
- The two OCC flavours stay distinct (ADR-0003): document-CAS conflicts still
  raise `OccConflict` from the handlers; transient driver conflicts are mapped to
  the same signal at the boundary, so both share the one bounded-retry path
  without conflation with the idempotency-key claim.
- `40001` is covered for completeness; under `READ COMMITTED` (ADR-0005) the
  engine does not raise serialization failures on the current paths, but a future
  stricter isolation level would be absorbed without further change.
- Scope is the transient-conflict codes only. A genuine invariant breach — a
  guard matching no row that should have — remains a separate, deliberately
  un-retried signal (tracked under issues #32 / #41); this record does not
  classify those.
