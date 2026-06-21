# 0022 — Return validation is per-line against shipped quantity, non-cumulative

- Status: Accepted
- Date: 2026-06-21

## Context

A return (customer RMA) is an inbound Receipt referencing the order it returns
(0008). Creating one needs a validation rule: what may be returned, and against
what. Options ranged from existence-only (any line for any order) to a fully
cumulative cap (total returned across all RMAs never exceeds total shipped),
which would require a per-line `returned_qty` ledger, a migration, and a guarded
write.

## Decision

Creating a return validates each line against the origin order's shipped
quantities:

- the origin order must exist (else `OrderNotFound`);
- the order must be in state `shipped` (else `ReturnNotAllowed`);
- each return line's SKU must have been shipped on that order, and the returned
  quantity must not exceed that line's shipped quantity (else `ReturnNotAllowed`).

Validation is **non-cumulative**: the engine does not track how much of an order
has already been returned across earlier RMAs. `ReturnNotAllowed` is a hard
rejection (idempotency-cached), because it is deterministic given the order's
terminal `shipped` state.

## Consequences

- "Cannot return what was not shipped" holds per request, matching physical
  reality, without a new column or migration.
- Because tracking is non-cumulative, multiple RMAs for one order can
  collectively return more than was shipped. This is an accepted limitation for
  this slice; a cumulative cap is deferred until a `returned_qty` ledger is
  justified, at which point a superseding record will be added.
- The catalog SKU existence check used for supplier receipts is unnecessary for
  returns: "must be a shipped line on the order" subsumes it.
- The order is unchanged by a return; it stays `shipped` (an RMA is an
  independent inbound document, per 0008), so there is no second restock path.
