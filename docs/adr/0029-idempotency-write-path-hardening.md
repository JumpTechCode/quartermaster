# 0029 — The idempotency write path is guarded against double-finalize and durable PENDING

- Status: Accepted
- Date: 2026-06-21

## Context

ADR-0004 fixed the idempotency caching policy; the envelope realizes it as a
two-phase claim/finalize inside one transaction (design §5.1). Two spots were
safe only by virtue of that single-transaction shape, and fragile to any future
change (issue #38).

- `finalize` was an unguarded `UPDATE idempotency_key ... WHERE key = :key` with
  no `status` predicate and no rowcount check. A second finalize for a key — if
  `claim` ever committed in a separate transaction, or a reaper called it — would
  silently overwrite a terminal record, corrupting every later replay.
- The replay branch handled only `REJECTED`, then `assert stored.response is not
  None` for the success case. There was no explicit `PENDING` handling, and a
  durable `PENDING` read back would trip the assert — which `python -O` strips,
  turning a defined error into undefined behavior.

Both are unreachable today: `claim` writes `PENDING` only as an uncommitted
intermediate, and the `INSERT ... ON CONFLICT DO NOTHING` claim blocks a
concurrent same-key duplicate until the first transaction resolves, so a row read
back on replay is always terminal. But "safe only because of an invariant
elsewhere" is exactly what a guard should make explicit.

## Decision

**Guard `finalize` and require exactly one row.** The update carries
`WHERE key = :key AND status = 'pending'` and raises `IdempotencyFinalizeError`
if it does not affect exactly one row. A terminal record can no longer be
overwritten; a miss is surfaced as a typed internal breach (an unmapped 500, not
a silent corruption) rather than passing unnoticed.

**Replace the success-branch assert with explicit status handling.** Replay now
returns the decoded response only for a `SUCCEEDED` row that has one; a durable
`PENDING` row, or a `SUCCEEDED` row missing its response, raises the typed
`IdempotencyInFlight`, mapped to **409 `idempotency_in_flight`** — "a request
with this key is in progress; retry to fetch the result" (the Stripe-style
convention). This is a latent branch (unreachable while claim/finalize share one
transaction); it is deliberate defensive contract, not dead code to be removed.

**Lock the exactly-once claim with a forced-interleaving test.** An integration
test holds two same-key transactions open at the claim step and asserts the
second blocks on the first, then either replays the committed result or claims
cleanly if the first rolled back — pinning the `ON CONFLICT DO NOTHING` blocking
behavior that the whole guarantee rests on.

## Consequences

- The idempotency write path is robust to a future multi-transaction claim
  (e.g. claim-commit-then-process) without silent record corruption or
  `-O`-dependent behavior; the guarantees no longer rest on an unstated invariant.
- A new public error code, `idempotency_in_flight` (409), joins the contract,
  latent under the current envelope but defined and tested.
- `finalize` can now raise; in the envelope it runs after the handler, outside the
  OCC try-block, so a (should-never-happen) raise rolls the transaction back and
  surfaces as a 500 rather than committing partial work. The ADR-0004 policy
  itself is unchanged.
