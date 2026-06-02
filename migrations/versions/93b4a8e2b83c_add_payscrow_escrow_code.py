"""add payscrow escrow_code

Revision ID: 93b4a8e2b83c
Revises: a1b2c3d4e5f6
Create Date: 2026-05-30 13:30:00.000000

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = '93b4a8e2b83c'
down_revision = 'a1b2c3d4e5f6'
branch_labels = None
depends_on = None


def upgrade():
    # Use raw SQL with IF NOT EXISTS to be idempotent on PostgreSQL.
    # The columns were already applied on production out-of-band, so this
    # prevents a DuplicateColumn error when re-running the migration chain.
    bind = op.get_bind()
    dialect = bind.dialect.name

    if dialect == 'postgresql':
        bind.execute(sa.text(
            "ALTER TABLE escrow_transactions ADD COLUMN IF NOT EXISTS "
            "payscrow_transaction_id VARCHAR(100)"
        ))
        bind.execute(sa.text(
            "ALTER TABLE escrow_transactions ADD COLUMN IF NOT EXISTS "
            "escrow_code VARCHAR(50)"
        ))
    else:
        # SQLite and others — use batch_alter_table (raises if column exists, but fine for fresh DBs)
        with op.batch_alter_table('escrow_transactions', schema=None) as batch_op:
            batch_op.add_column(sa.Column('payscrow_transaction_id', sa.String(length=100), nullable=True))
            batch_op.add_column(sa.Column('escrow_code', sa.String(length=50), nullable=True))


def downgrade():
    with op.batch_alter_table('escrow_transactions', schema=None) as batch_op:
        batch_op.drop_column('escrow_code')
        batch_op.drop_column('payscrow_transaction_id')
