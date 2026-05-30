"""Bridge c2e1c072881a to known chain

Revision ID: c2e1c072881a
Revises: e0cc9c47dfa0
Create Date: 2026-05-30 11:00:00.000000

This migration exists to bridge the production database which recorded
c2e1c072881a as its current revision back to our known migration chain.
It is a no-op.
"""
from alembic import op
import sqlalchemy as sa

revision = 'c2e1c072881a'
down_revision = 'e0cc9c47dfa0'
branch_labels = None
depends_on = None

def upgrade():
    pass

def downgrade():
    pass
