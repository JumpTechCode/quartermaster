# 0018 — Reaper ledger semantics

- Status: Accepted
- Date: 2026-06-20

## Context

The reservation reaper (0010) releases stock for an expired reservation and must
record the change in the append-only `movement` ledger, like every other stock
mutation. But a reaper action is not a client command: it has no idempotency key,
and the `movement.command_id` column is `NOT NULL`. It also needs to be
distinguishable, after the fact, from an explicit `cancel` — both lower
`qty_reserved` and free a held reservation.

## Decision

- Add a distinct **`EXPIRE`** movement type rather than reusing `RELEASE`, so the
  ledger tells a time-based expiry apart from a client cancel without inspecting
  other columns.
- Reaper movements carry a **synthetic, deterministic `command_id`** of the form
  `reaper:expire:<reservation_id>`, satisfying the `NOT NULL` column and tracing
  each ledger row back to the reaper action that wrote it. The value cannot
  collide with a client idempotency key in any meaningful way — `command_id` on
  `movement` is descriptive, not a uniqueness constraint.
- The reaper **bypasses the idempotency envelope**. It claims no key; its
  exactly-once guarantee comes from the reservation-state CAS (`held → expired`),
  exactly as the `cancel` handler relies on the same CAS. A concurrent reaper or
  a racing `cancel` simply finds zero rows to transition and is a defined no-op
  (0005: the conditional `WHERE` is the guard).

This records the mechanism; 0010 remains the policy ADR (the TTLs and that a
reaper releases) and is unchanged.

## Consequences

- The conservation oracle sums movements regardless of type, so `EXPIRE` needs no
  special handling there; it is purely an audit/observability distinction.
- Reaper writes need no idempotency-table row, so they add no contention to the
  unique-index INSERT that serialises real commands.
- The `EXPIRE` value is added to `MovementType`; the `movement.type` CHECK
  constraint, generated from the enum, accepts it automatically.
