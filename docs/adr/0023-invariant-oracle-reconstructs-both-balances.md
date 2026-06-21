# 0023 — The invariant oracle reconstructs both balances from the ledger; exactly-once is out-of-band

- Status: Accepted
- Date: 2026-06-21

## Context

ADR-0011 established that conservation is verified by an offline oracle, never on
the command path. This record fixes how that oracle works, now that it is built.

The `movement` ledger records every stock state change with a `type`, a positive
`qty`, and directional `from_location` / `to_location`. The on-hand-affecting
types are `RECEIVE` (+to), `PUTAWAY` (−from, +to), and `PICK` (−from); the
reserved-affecting types are `RESERVE` (+to), `RELEASE` (−from), `EXPIRE`
(−from), and `PICK` (−from, since a pick consumes its reservation). The design
spec §7 headline framed conservation as on-hand per SKU, but the ledger already
carries directional reserved-side movements, so both balances are reconstructable
per `(sku, location)` cell.

## Decision

- The oracle reconstructs **both** on-hand and reserved balances **per
  `(sku, location)` cell** from the ledger and compares each against the live
  `stock` row — stronger than the spec's on-hand-per-SKU headline, at no extra
  cost because the data is already recorded.
- The `type → effect` mapping above **is** the conservation definition. It lives
  in the application layer (`application/oracle.py`) as a pure function over the
  adapter's `GROUP BY (type, sku, from, to)` totals, so it is exhaustively
  unit-testable without a database; the adapter does only the SQL grouping.
- `no_oversell` is stated precisely as `ever_received ≥ shipped + on_hand_total`
  per SKU (`ever_received = Σ RECEIVE`). The design spec's "shipped + on-hand +
  reserved ≤ ever-received" double-counted: `reserved ⊆ on_hand` (storage CHECK
  `qty_reserved <= qty_on_hand`), so the `+ reserved` term is dropped.
- **Exactly-once is verified out-of-band, not as a post-hoc query.** It is
  demonstrated by the concurrency integration tests (one effect per idempotency
  key fired K times) and would surface here as conservation drift or oversell on
  a double-apply. The report lists it as `NOT_CHECKED` rather than omitting it.
- The oracle reads through dedicated read-only aggregate ports
  (`StockRepo.all_cells`, `MovementRepo.aggregate`, `OrderRepo.shipped_by_sku`,
  `OrderRepo.lines_breaking_monotonic`), distinct from the conditional-write
  methods, so it is an independent witness of the write path it audits.

This complements ADR-0011 (which establishes conservation as offline); it does
not supersede it.

## Consequences

- The oracle catches lost-update / double-apply bugs the storage CHECKs cannot,
  on both the on-hand and reserved axes, located to the exact cell.
- `stock_bounds` (`0 ≤ reserved ≤ on_hand`) and `state_integrity`
  (`0 ≤ shipped ≤ picked ≤ allocated ≤ ordered`) are independent re-checks of
  invariants the storage CHECKs already enforce; they add a second witness but
  little new coverage.
- `no_oversell` cannot fail in isolation in a consistent store — it is implied by
  `conservation_on_hand` ∧ `state_integrity` — so a real single corruption that
  breaks it also breaks conservation; it is exercised in isolation only in unit
  tests, which supply the inputs independently.
- The oracle remains offline and read-only; production never sums the ledger, and
  rolling-checkpoint snapshots stay deferred (ADR-0011).
