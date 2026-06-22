# 0024 — Stock-guard rejections are classified: client conflict (409) vs. invariant breach (500)

- Status: Accepted
- Date: 2026-06-21

## Context

The command handlers mutate stock with invariant-guarded conditional writes: the
SQL `WHERE` is the guard, and a write that matches no rows returns a 0 rowcount.
Four handlers turned that 0-rowcount into a raised `InvariantViolation`
(`pick`, `cancel`, `receive`, `putaway`). `InvariantViolation` appeared in neither
`HARD_REJECTION` nor `TRANSIENT` in the envelope, and was absent from the API
status map, so it escaped the envelope's `try` (rolling back, finalizing nothing)
and reached the catch-all as an opaque `500 internal_error`.

That conflated two structurally identical outcomes (a guard rejected a write) that
are semantically very different:

- **A genuine consistency breach.** In `pick`/`cancel`, the per-reservation stock
  change runs only *after* this actor won the reservation-state CAS
  (`held → consumed`/`released`). Reaching the 0-rowcount means an actor holds a
  reservation whose backing stock is gone — a reservation/stock divergence that
  should never happen under correct operation. It is a server-side correctness
  alarm.
- **A foreseeable client/concurrency conflict.** In `putaway`, `from_location` is
  a free request field (the fungible-cell model — see #76). Pointing it at a cell
  that lacks the unreserved stock makes `remove_on_hand` match no rows. That is
  ordinary client/concurrency input, not a server fault — and surfacing it as a
  `500` reads as a spurious fault (especially to the load harness, which calls
  handlers directly).

`receive`'s `add_received` guard sits under the document CAS where the receipt is
single-writer; its only below-API trigger is duplicate-SKU input, which #74 makes
a deterministic `InvalidReceiptLine` *before* the guard is reached. So after #74
its residual 0-rowcount is, like `pick`/`cancel`, a true breach.

## Decision

Split the two conditions into two error families and classify each explicitly:

- **`StockConflict`** — a stock guard rejected an operation on otherwise-valid
  input (a cell lacking the unreserved stock to move). `putaway` raises it instead
  of `InvariantViolation`. It is **`TRANSIENT`**: the envelope rolls back (so any
  lines already moved in the loop are discarded) and re-raises without finalizing,
  joining `InsufficientStock` — both are "not enough stock right now" outcomes a
  retry may clear, not consistency breaches. It maps to **`409 stock_conflict`**.
- **`InvariantViolation`** — a genuine breach (`pick`/`cancel`, and `receive`'s
  residual guard). The envelope catches it explicitly, **rolls back, and never
  finalizes**: a server-side alarm is not a business rejection, and caching it
  would both mislabel it and (mid-loop) risk committing partial state. It maps to
  a **classified `500 invariant_violation`** with a generic body — distinct from
  the opaque `internal_error` catch-all so it is greppable and alertable, but the
  internal detail (which reservation, which cell) is not surfaced to the client.

The in-gate conditional `WHERE` remains the authoritative guard in every case; this
record only fixes how a guard rejection is *classified and surfaced*.

This refines ADR-0004 (idempotency caching): a true invariant breach is the one
handler-raised outcome that is neither cached (like a hard rejection) nor a normal
transient business failure — it is rolled back and surfaced as an alarm.

## Consequences

- `putaway` against a cell that lacks the stock is a clean `409`, replayable as a
  fresh command, instead of an opaque `500`. The load harness no longer reads a
  foreseeable client/concurrency condition as a server fault.
- A real reservation/stock divergence is a loud, classified, recorded `500` rather
  than an indistinguishable crash, without leaking internals to clients.
- `StockConflict` is not cached. A retry under the *same* idempotency key re-runs
  rather than replaying a stored rejection; this matches `InsufficientStock` and is
  correct because the shortfall is concurrency-sensitive, not a deterministic
  property of the command.
- The envelope's exception ladder now has four arms (OCC retry, transient
  roll-back, invariant-breach roll-back, hard-rejection finalize), each with a
  distinct idempotency outcome.
