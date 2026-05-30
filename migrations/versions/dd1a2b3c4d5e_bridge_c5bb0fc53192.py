"""Bridge unknown production revision c5bb0fc53192 into the known chain.

Revision ID: dd1a2b3c4d5e
Revises: c5bb0fc53192
Create Date: 2026-05-30 16:00:00.000000

The live production database has recorded 'c5bb0fc53192' as its current
alembic revision, but this revision was never committed to the codebase.
This migration makes 'c5bb0fc53192' the down_revision so Alembic can
find it and continue from here.

This is a no-op — no schema changes are made. It purely fixes the
migration history chain so that the app boots cleanly.
"""
from alembic import op
import sqlalchemy as sa

revision = 'dd1a2b3c4d5e'
down_revision = 'c5bb0fc53192'
branch_labels = None
depends_on = None


def upgrade():
    pass


def downgrade():
    pass
