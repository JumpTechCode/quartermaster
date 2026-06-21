# 19. Reaper de-allocates the owning order (allocated → backordered)

Date: 2026-06-21

## Status

Accepted

## Context

The reservation-expiry reaper (ADR-0017/0018) releases a `held` reservation past
its TTL: `held → expired`, `qty_reserved` lowered, an `EXPIRE` movement appended.
It did not touch the owning order. An `allocated` order whose reservation is
reaped was therefore left in `allocated` with its `allocated_qty` intact but no
held reservation: invisible to the backorder sweep (which scans only
`backordered`), and — if subsequently picked — advancing
`allocated → picking → picked` while consuming nothing, a silent under-ship that
breaks no per-line invariant (`picked ≤ allocated`, `shipped ≤ picked` still
hold).

## Decision

When the reaper wins a reservation's `held → expired` CAS it now de-allocates the
owning order in the same transaction:

1. lower the line's `allocated_qty` by the reservation quantity, guarded by
   `allocated_qty - qty >= picked_qty`; a rejected guard is a corruption alarm
   (it cannot happen — winning the expiry CAS means the quantity was reserved and
   not picked), surfaced as a logged, counted error, not a retry;
2. flip the order `allocated → backordered` with a state-only guarded write
   (`WHERE state = 'allocated'`), best-effort: if the order has concurrently moved
   to `picking`/`cancelled`, or a sibling reservation already re-opened it, the
   flip is a no-op.

A new `allocated → backordered` transition is added to the order state machine;
no command path emits it. The backorder sweep then re-allocates the re-opened
order from the freed stock.

The reservation-state CAS remains the single arbiter of a reservation's
disposition, so de-allocation is correct-by-construction against concurrent
`pick`, `cancel`, and other reaper passes: whoever wins the CAS owns the stock and
order-line bookkeeping; the loser is a defined no-op.

## Consequences

- Reaped orders regain liveness (re-allocated once stock is available) and the
  silent-under-ship-on-pick window is closed for still-`allocated` orders.
- A reservation reaped mid-`pick` still results in a partial pick / under-ship —
  the correct, already-legal outcome, since there is no `picking → backordered`
  transition and cancel is release-only/pre-pick.
- No schema change (no new column, index, or enum value); no movement-ledger
  change, so the conservation oracle is unaffected (the `EXPIRE` movement, per
  ADR-0018, remains the sole ledger entry). This record governs the order-state
  consequence; ADR-0018 governs the ledger entry.
