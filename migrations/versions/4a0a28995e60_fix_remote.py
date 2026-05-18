"""fix missing revision

Revision ID: 4a0a28995e60
Revises: ea362e02b64f
Create Date: 2026-05-18 11:47:00.000000

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = '4a0a28995e60'
down_revision = ('ea362e02b64f', 'f06997b26349')
branch_labels = None
depends_on = None


def upgrade():
    pass


def downgrade():
    pass
