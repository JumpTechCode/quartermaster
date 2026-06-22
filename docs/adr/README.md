# Architecture Decision Records

This directory records the significant, hard-to-reverse decisions made while
building Quartermaster. Each record captures the context, the decision, and its
consequences, so the reasoning is available later even when the people change.

The format follows Michael Nygard's
[Documenting Architecture Decisions](https://cognitect.com/blog/2011/11/15/documenting-architecture-decisions.html).

## Index

- [0001 — Record architecture decisions](0001-record-architecture-decisions.md)
- [0002 — Relational, transactional core over event sourcing](0002-relational-transactional-core.md)
- [0003 — Two flavours of optimistic concurrency control](0003-two-flavour-occ.md)
- [0004 — Idempotency caches successes and hard rejections; business failures roll back](0004-idempotency-caching-policy.md)
- [0005 — READ COMMITTED with conditional writes on the hot paths](0005-read-committed-conditional-writes.md)
- [0006 — On-hand / reserved / available reservation model with line-level partiality](0006-reservation-model-line-partiality.md)
- [0007 — Inbound and outbound flows are decoupled](0007-inbound-outbound-decoupling.md)
- [0008 — Returns are inbound RMA documents that reuse the Receipt lifecycle](0008-returns-as-inbound-rma.md)
- [0009 — Order cancellation is release-only (pre-pick states)](0009-order-cancel-release-only.md)
- [0010 — Time-to-live and background reapers for reservations and idempotency keys](0010-ttl-reapers.md)
- [0011 — Conservation is verified by an offline oracle, not on the command path](0011-conservation-offline-oracle.md)
- [0012 — Architectural boundaries are enforced by import-linter](0012-import-linter-boundaries.md)
- [0013 — Entity identity typing: NewType over primitives, natural codes, UUID document IDs](0013-entity-identity-typing.md)
- [0014 — Postgres-early testing with testcontainers](0014-postgres-early-testcontainers.md)
- [0015 — UUIDv7 for document identifiers, generated app-side](0015-uuidv7-document-ids.md)
- [0016 — Atomic partial-reserve primitive for greedy allocation](0016-atomic-partial-reserve-primitive.md)
- [0017 — Polled worker execution model](0017-worker-execution-model.md)
- [0018 — Reaper ledger semantics](0018-reaper-ledger-semantics.md)
- [0019 — Reaper de-allocates the owning order (allocated → backordered)](0019-reaper-deallocates-order.md)
- [0020 — Translate transient Postgres conflicts to OccConflict at the adapter boundary](0020-translate-transient-conflicts-to-occ.md)
- [0021 — The movement ledger carries no uniqueness constraint; append-once is a CAS-gate property](0021-movement-ledger-no-uniqueness.md)
- [0022 — Return validation is per-line against shipped quantity, non-cumulative](0022-return-validation-against-shipped.md)
- [0023 — The invariant oracle reconstructs both on-hand and reserved from the ledger; exactly-once is out-of-band](0023-invariant-oracle-reconstructs-both-balances.md)
- [0024 — Stock-guard rejections are classified: client conflict (409) vs. invariant breach (500)](0024-stock-guard-error-taxonomy.md)
- [0025 — Read transactions run at REPEATABLE READ for a single-snapshot view](0025-read-paths-repeatable-read-snapshot.md)

## Adding a record

Copy the structure of an existing record, give it the next number, and set its
status. Records are immutable once accepted: to change a decision, add a new
record that supersedes the old one and update the older record's status.
