# 0016 — Atomic partial-reserve primitive for greedy allocation

- Status: Accepted
- Date: 2026-06-17

## Context

Allocating an order line greedily across a SKU's locations needs to reserve
`min(want, available)` at each location, atomically and without a
read-modify-write gap. The all-or-nothing conditional write used elsewhere
(`UPDATE stock SET qty_reserved = qty_reserved + :n WHERE available >= :n`)
cannot express a partial fill: it either reserves the whole requested quantity
or nothing. Under `READ COMMITTED`, reading availability and then writing in two
statements would admit a lost update between the read and the write.

## Decision

Reserve at one location with a single statement that locks the row, computes the
take against the locked committed value, and applies it:

    WITH picked AS (
        SELECT sku_id, location_id, LEAST(:want, qty_on_hand - qty_reserved) AS take
          FROM stock
         WHERE sku_id = :sku AND location_id = :loc AND qty_on_hand - qty_reserved > 0
         FOR UPDATE
    )
    UPDATE stock s SET qty_reserved = s.qty_reserved + p.take
      FROM picked p WHERE s.sku_id = p.sku_id AND s.location_id = p.location_id
    RETURNING p.take;

`FOR UPDATE` takes the row lock; under `READ COMMITTED` the lock acquisition
re-reads the latest committed row, so `LEAST` is evaluated against the true
current availability; `RETURNING p.take` yields the amount reserved (0 when no
row qualifies). The greedy handler loops this primitive over the SKU's locations
in `location_id` order until the line is satisfied or the locations are
exhausted.

## Consequences

Concurrent reservers on the same cell serialize on the row lock, each
recomputing `take` against the reduced availability, so the sum of reserves can
never exceed on-hand — no oversell, demonstrated by the N-concurrent-allocate
race. No per-location retry loop is needed; one statement is the unit of
atomicity. The `qty_reserved <= qty_on_hand` CHECK remains the storage-layer
backstop. This primitive is distinct from, and coexists with, the all-or-nothing
conditional write used where partiality is not wanted (e.g. a future
single-location pick).
