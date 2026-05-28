"""Bridge to production revision

Revision ID: e0cc9c47dfa0
Revises: 
Create Date: 2026-05-28 00:00:00.000000

This migration exists purely to bridge the gap between what the production
database recorded as its last applied revision (e0cc9c47dfa0) and the
local migration chain. It is a no-op — all schema changes were applied
directly to production before the migration history was set up here.
"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'e0cc9c47dfa0'
down_revision = None
branch_labels = None
depends_on = None


def upgrade():
    # No-op: production schema already has these tables.
    # This revision exists only to satisfy the alembic_version pointer
    # that the production RDS instance recorded.
    pass


def downgrade():
    pass
