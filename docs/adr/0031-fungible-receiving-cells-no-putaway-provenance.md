# 0031 — Receiving cells are fungible (sku, location) staging; putaway trusts from_location

- Status: Accepted
- Date: 2026-06-22

## Context

Stock is modelled per `(sku, location)` cell. `receive` lands a receipt's
quantities at `command.location_id` (a non-shelf staging cell) and records the
quantity on `receipt_line.received_qty`, but persists the *receiving location*
nowhere on the receipt or receipt_line. `putaway` then takes `from_location`
entirely from the caller and moves `line.received` out of that cell, guarded only
by `remove_on_hand`'s `qty_on_hand - qty_reserved >= qty` cell check (ADR-0024) —
with no verification that `from_location` is the cell this particular receipt
actually used.

A consequence (raised as #76): if a caller passes a wrong-but-populated receiving
cell, `putaway` silently relocates some *other* receipt's staged stock out of that
cell, tags the `PUTAWAY` movement with the wrong receipt, and strands the
originating receipt's stock at its real cell. Per-cell conservation still balances
(the units moved really were at the named cell), so the offline oracle
(`run_oracle`) cannot witness the misattribution.

The question was whether `putaway` should validate receiving-location provenance,
or whether the fungible-cell model is deliberate.

## Decision

The fungible `(sku, location)` receiving-cell model is **intentional**. A
receiving cell is shared staging space, not per-receipt-owned storage:

- Stock at a receiving cell is fungible across receipts. No ADR or design
  promise has ever attached per-receipt ownership to a staged unit, and the data
  model deliberately has no receiving-location column on the receipt to validate
  against.
- `putaway` continues to trust `command.from_location`, guarded only by
  `remove_on_hand`'s cell check. A wrong-but-valid `from_location` is the same
  class of free-field input as the putaway `StockConflict` case in ADR-0024 — it
  cannot breach a core guarantee (no oversell, no conservation breach, no lost
  update); its only effect is attribution/stranding within already-staged stock.
- We therefore do **not** record a receiving location on the receipt/receipt_line
  and do **not** add a provenance guard to `putaway`. Adding per-receipt
  provenance (a receiving-location column + a putaway check) is deliberately out
  of scope.

## Consequences

- No schema or handler change; the receive/putaway path is unchanged.
- The oracle cannot witness putaway misattribution (per-cell reconstruction
  matches the movements actually issued), and this is an accepted limit, not a
  defect — recorded here so it is not mistaken for an oracle gap. It is distinct
  from the reserved-side gap closed by `reservation_reconciliation` (ADR-0023,
  #68), which was a true divergence; misattribution leaves every balance correct.
- The load harness passes the **correct** `from_location` for each receipt and
  does not fuzz this axis, because the model has no per-receipt provenance to
  assert against. The harness's adversarial input is constrained on this one axis
  by design, the same way it caps one RMA per `(origin_order, sku)` for the
  ADR-0022 deferral.
- If per-receipt provenance is ever wanted, it is a clean future record that
  supersedes this one: add a receiving-location column written at receive time and
  have putaway derive/validate `from_location` from it.
