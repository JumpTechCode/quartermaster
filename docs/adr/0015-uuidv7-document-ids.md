# 0015 — UUIDv7 for document identifiers, generated app-side

- Status: Accepted
- Date: 2026-06-17

## Context

ADR-0013 committed the domain's document ids (order, receipt, reservation,
movement) to synthetic UUIDs but deferred the generation strategy (v4 / v7 /
bigint) to the persistence layer. These ids are primary keys on high-write
tables — the movement ledger especially is append-only and the highest-volume
table in the system. The strategy affects index locality and write throughput,
which matter because the engine is required to stay correct *and* fast under
concurrent load.

## Decision

- **UUIDv7, generated app-side.** Document ids are time-ordered UUIDv7 values
  minted in Python via `uuid-utils` (Python 3.13's stdlib ships only
  uuid1/3/4/5). The value is returned as a stdlib `uuid.UUID` so the rest of the
  code stays on the standard type.
- **App-side, not a database `DEFAULT`.** The command envelope mints the id
  before insert so it can reference the document from its movement-ledger rows
  within the same transaction.
- **bigint excluded** because ADR-0013 already commits the domain to
  UUID-typed ids; switching would reopen it.

## Consequences

- Time-ordered keys insert near the right edge of the primary-key B-tree, giving
  better index locality and less fragmentation than random v4 — most valuable on
  the append-only movement ledger under sustained concurrent writes.
- One additional runtime dependency (`uuid-utils`, a compiled wheel) enters the
  supply chain; it is covered by pip-audit like every other dependency.
- App-side generation keeps the id available for in-transaction references
  (movements) without a `RETURNING` round-trip.
