# 21. The movement ledger carries no uniqueness constraint; append-once is a CAS-gate property

Date: 2026-06-21

## Status

Accepted

## Context

The `movement` ledger is append-only and is summed by the offline conservation
oracle (ADR-0011), so a duplicated row would corrupt reconciliation. That invites
a storage-level append-once constraint — a unique index over the command/effect
identity (issue #39) — matching how the other inventory invariants are
DB-enforced rather than merely relied upon.

But no business-column key can carry it, for two structural reasons:

1. **A command appends many movements.** `allocate` emits one `RESERVE` per
   `(sku, location)` it draws from; `pick`/`cancel` emit one `PICK`/`RELEASE` per
   held reservation; `receive`/`putaway` one per line. So the suggested
   `(command_id, type, sku_id, ref)` collides on a *legitimate* multi-location
   allocation — every column equal but `to_location`.

2. **Some legitimate movements are business-identical.** Adding the locations
   fixes allocate but not `pick`/`cancel`: an order can hold two `held`
   reservations at the *same* `(sku, location)` — partial-allocate at L →
   backordered → restock at L → the backorder sweep (ADR-0019/§ sweep) re-allocates
   at L — and `pick`/`cancel` then append two rows identical on
   `(command_id, type, sku_id, from_location, to_location)`, differing only by the
   `movement_id` primary key (and not always by `qty`). No business key
   distinguishes them, so any such unique index would reject a valid command.

This is why the design has always treated `movement.command_id` as descriptive,
with no FK and no uniqueness.

## Decision

Do not add a uniqueness constraint to the `movement` ledger. Append-once is
enforced upstream, in two layers that already exist:

1. **Idempotency (ADR-0004).** A duplicate command replays the stored response
   and never re-executes its body, so the movements are never re-appended.

2. **The document state-CAS gates.** Even if the idempotency layer were bypassed,
   a replay finds the aggregate already transitioned — `allocated → …`,
   `arrived → …`, `held → consumed/released/expired` — and the guarded CAS makes
   the append a defined no-op (the reservation/receipt/order CAS matches no row)
   or rolls the whole single-transaction command back. The movement append and
   the CAS that authorises it commit, or roll back, together.

The `movement_id` primary key (UUIDv7, app-side — ADR-0015) keeps rows
individually addressable and time-ordered; it is deliberately *not* a dedup key
(a replay would mint a fresh id) and is not relied on as one.

## Consequences

- The conservation oracle's inputs stay duplicate-free by the pipeline's
  exactly-once guarantee, not by a storage constraint — consistent with the rest
  of the engine, where the guard is the conditional write / CAS (ADR-0003,
  ADR-0005), and uniqueness is the guard only where it can be (the idempotency-key
  `INSERT`, ADR-0004).
- This records a deliberate *non-addition*, so issue #39's suggested index is not
  silently adopted and the question does not recur. Shipping it would have turned
  a foreseeable, valid `pick`/`cancel` into a storage-layer rejection — worse than
  the duplicate it guards against, which the two layers above already preclude.
- If a future change gives each effect a replay-stable, storable identity (a
  deterministic effect key), a qualified unique index could be revisited under a
  superseding record. Until then, the ledger's correctness rests on the gates,
  not on storage uniqueness.
