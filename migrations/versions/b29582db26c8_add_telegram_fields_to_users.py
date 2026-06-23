"""Add telegram_id and telegram_notification_prefs to users table.

Revision ID: b29582db26c8
Revises: 05672193aa5b
Create Date: 2026-06-23 04:00:00.000000

"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.engine.reflection import Inspector

# revision identifiers, used by Alembic.
revision = 'b29582db26c8'
down_revision = '05672193aa5b'
branch_labels = None
depends_on = None


def upgrade():
    conn = op.get_bind()
    inspector = Inspector.from_engine(conn)
    columns = [c['name'] for c in inspector.get_columns('users')]
    
    if 'telegram_id' not in columns:
        op.add_column('users', sa.Column('telegram_id', sa.String(length=50), nullable=True))
    
    if 'telegram_notification_prefs' not in columns:
        op.add_column('users', sa.Column('telegram_notification_prefs', sa.JSON(), nullable=True))
        
    indexes = [i['name'] for i in inspector.get_indexes('users')]
    if 'ix_users_telegram_id' not in indexes:
        op.create_index('ix_users_telegram_id', 'users', ['telegram_id'], unique=True)


def downgrade():
    op.drop_index('ix_users_telegram_id', table_name='users')
    op.drop_column('users', 'telegram_notification_prefs')
    op.drop_column('users', 'telegram_id')
