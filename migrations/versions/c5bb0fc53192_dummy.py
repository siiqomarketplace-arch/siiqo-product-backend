"""Dummy revision to complete the local migration history chain.

Revision ID: c5bb0fc53192
Revises: c2e1c072881a
"""
from alembic import op
import sqlalchemy as sa

revision = 'c5bb0fc53192'
down_revision = 'c2e1c072881a'
branch_labels = None
depends_on = None

def upgrade():
    pass

def downgrade():
    pass
