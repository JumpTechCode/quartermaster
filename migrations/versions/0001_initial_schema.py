# migrations/versions/0001_initial_schema.py
"""initial schema — the §3 data model.

Revision ID: 0001_initial
Revises:
Create Date: 2026-06-17

The first migration builds exactly the Core metadata, so the schema has one
source of truth (``tables.py``) and the drift test is meaningful for every
later, hand-written revision.
"""

from __future__ import annotations

from alembic import op

from quartermaster.adapters.postgres.tables import metadata

revision = "0001_initial"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    metadata.create_all(bind=op.get_bind(), checkfirst=False)


def downgrade() -> None:
    metadata.drop_all(bind=op.get_bind(), checkfirst=False)
