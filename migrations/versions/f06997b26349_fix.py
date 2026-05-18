"""fix missing migration f06997b26349

Revision ID: f06997b26349
Revises: b7201c8e9fa1
Create Date: 2026-05-18 10:25:00.000000

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'f06997b26349'
down_revision = 'b7201c8e9fa1'
branch_labels = None
depends_on = None


def upgrade():
    # Dummy migration to satisfy alembic versioning mismatch
    pass


def downgrade():
    pass
