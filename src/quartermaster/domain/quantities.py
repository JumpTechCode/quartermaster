"""The shared upper bound on inventory quantities.

Every quantity the engine tracks — ordered/allocated/picked/shipped on a line,
expected/received on a receipt, on-hand/reserved on a stock cell — is stored in a
32-bit ``integer`` column (design spec §3). ``MAX_QTY`` is that signed 32-bit
ceiling. The request models reject anything larger at the boundary (a ``422``
rather than an ``INSERT``-time overflow surfacing as a ``500``), and the domain
constructors mirror the bound so the in-memory types and the database columns
agree on what a representable quantity is.
"""

from __future__ import annotations

# The largest value a signed 32-bit Postgres ``integer`` column can hold.
MAX_QTY = 2_147_483_647
