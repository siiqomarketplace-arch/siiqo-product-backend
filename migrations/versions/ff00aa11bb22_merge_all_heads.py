"""Merge all branch heads into single clean linear chain.

Revision ID: ff00aa11bb22
Revises: 20eee4b60e25, 93b4a8e2b83c, dd1a2b3c4d5e
Create Date: 2026-05-30 16:01:00.000000

Merges three concurrent branch heads:
  - 20eee4b60e25 (negotiation tables)
  - 93b4a8e2b83c (payscrow escrow_code)
  - dd1a2b3c4d5e (bridge for c5bb0fc53192)

into a single linear head. All schema changes were already applied.
This is a no-op merge migration.
"""
from alembic import op
import sqlalchemy as sa

revision = 'ff00aa11bb22'
down_revision = ('20eee4b60e25', '93b4a8e2b83c', 'dd1a2b3c4d5e')
branch_labels = None
depends_on = None


def upgrade():
    pass


def downgrade():
    pass
