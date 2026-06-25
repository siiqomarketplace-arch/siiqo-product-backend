"""Merge telegram and category attribute heads

Revision ID: b0b7207cb4be
Revises: a1c2d3e4f5a6, b29582db26c8
Create Date: 2026-06-25 12:15:46.335441

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'b0b7207cb4be'
down_revision = ('a1c2d3e4f5a6', 'b29582db26c8')
branch_labels = None
depends_on = None


def upgrade():
    pass


def downgrade():
    pass
