"""Add telegram_id and telegram_notification_prefs to users table.

Revision ID: a1b2c3d4e5f6
Revises: ff00aa11bb22
Create Date: 2026-06-23 04:00:00.000000

"""
from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision = 'a1b2c3d4e5f6'
down_revision = 'ff00aa11bb22'
branch_labels = None
depends_on = None


def upgrade():
    # Add telegram_id — unique, nullable, indexed
    with op.batch_alter_table('users', schema=None) as batch_op:
        batch_op.add_column(
            sa.Column('telegram_id', sa.String(length=50), nullable=True)
        )
        batch_op.add_column(
            sa.Column('telegram_notification_prefs', sa.JSON(), nullable=True)
        )
        batch_op.create_unique_constraint('uq_users_telegram_id', ['telegram_id'])
        batch_op.create_index('ix_users_telegram_id', ['telegram_id'], unique=True)


def downgrade():
    with op.batch_alter_table('users', schema=None) as batch_op:
        batch_op.drop_index('ix_users_telegram_id')
        batch_op.drop_constraint('uq_users_telegram_id', type_='unique')
        batch_op.drop_column('telegram_notification_prefs')
        batch_op.drop_column('telegram_id')
