"""Add flw_subaccount_id to vendor_bank_accounts

Revision ID: b1c2d3e4f5a7
Revises: b29582db26c8
Create Date: 2026-09-24 00:00:00.000000

Adds flw_subaccount_id (nullable string) to vendor_bank_accounts.
Stores the Flutterwave collection subaccount ID (RS_xxx) for each vendor
so that Flutterwave checkout can split payments natively to the vendor's
bank account at settlement.

NOTE: down_revision set to b29582db26c8 (one of the three production heads).
This migration is a simple nullable ADD COLUMN — safe to apply directly.
"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'b1c2d3e4f5a7'
down_revision = ('b29582db26c8', 'a1c2d3e4f5a6', 'b0b7207cb4be')
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table('vendor_bank_accounts', schema=None) as batch_op:
        batch_op.add_column(
            sa.Column('flw_subaccount_id', sa.String(length=120), nullable=True)
        )


def downgrade():
    with op.batch_alter_table('vendor_bank_accounts', schema=None) as batch_op:
        batch_op.drop_column('flw_subaccount_id')
