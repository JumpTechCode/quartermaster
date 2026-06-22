# 0025 — Read transactions run at REPEATABLE READ for a single-snapshot view

- Status: Accepted
- Date: 2026-06-21

## Context

ADR-0005 pins the hot command paths to `READ COMMITTED`: every stock mutation is
an invariant-guarded conditional write, so the in-gate `WHERE` re-checks against
the locked committed row and there is no read-modify-write gap to protect. That
reasoning is scoped to *write* paths.

Several read paths, however, issue multiple independent `SELECT`s inside one
transaction:

- `load_order` reads the order header (`orders.get`) then its lines
  (`orders.get_lines`); `load_receipt` has the identical shape.
- `run_oracle` reads four base tables (movement aggregate, all stock cells,
  shipped-by-sku, monotonic-breaking lines) and cross-checks them against one
  another.

Under `READ COMMITTED` each statement takes a fresh snapshot. A command
committing between two statements is partially visible: the API can return a
post-commit header/`version` paired with pre-commit line quantities — a view
that never atomically coexisted — and the oracle can fold aggregates taken at two
instants into a torn cross-check, producing a false FAIL on consistent data (or,
via a compensating pair, masking real drift). No ADR covered read-side snapshot
consistency, and the inconsistency was unguarded. (Issue #70.)

The impact is bounded — the reads are read-only, no command consumes a
client-supplied `version` as an OCC token, and the oracle reconstructs from base
tables — but the oracle is the project's proof instrument, and the load harness
is exactly the context where running reads against live traffic is tempting.

## Decision

Read transactions run at `REPEATABLE READ`, via a dedicated
`postgres_read_uow_factory` that pins `isolation_level="REPEATABLE READ"` on the
connection for the life of the transaction. Every statement in the unit of work
then shares the one MVCC snapshot taken at its first read, so a multi-statement
view is internally consistent regardless of commits that land mid-read.

The API read routes (`GET /orders/{id}`, `GET /receipts/{id}`) take this factory
through a separate `Deps.read_uow_factory` seam; the command routes keep the
`READ COMMITTED` `uow_factory`. `run_oracle` documents that it requires a
single-snapshot factory and is given the read factory by its callers.

The pin is per transaction and resets on return to the pool, so it never touches
the guarded write paths. This does not weaken ADR-0005: writes stay
`READ COMMITTED`; this record only adds a stronger level for the read side, where
the guarantee is snapshot consistency across statements rather than guarded
single-row writes.

## Consequences

- API read endpoints return a self-consistent header/lines/`version` triple, and
  the oracle's cross-checks fold over one instant — robust even if a future
  caller runs it against a live store rather than a quiesced one.
- A second isolation level now exists in the adapter. The split is explicit (two
  named factories, two `Deps` seams) so the choice is visible at every call site;
  reads must route through `read_uow_factory`, not `uow_factory`.
- `REPEATABLE READ` read transactions are read-only and never commit, so they
  cannot raise the `40001` serialization failures ADR-0005 avoids on writes.
- The guarantee still depends on discipline: a read path wired to the write
  factory would silently reintroduce the torn-read window.
