"""Add paystack_subaccount_code for Split Payments

Revision ID: a9b8c7d6e5f4
Revises: 3a4b5c6d7e8f
Create Date: 2026-07-07 11:00:00.000000

Adds paystack_subaccount_code to:
  - storefronts          (set during vendor onboarding)
  - vendor_bank_accounts (set when vendor adds bank account in settings)

These codes are used by the Paystack Split Payments feature so that
digital/service checkout transactions are split natively at the gateway,
routing Siiqo's 12% fee to the main Siiqo account and the vendor's share
directly to their Paystack subaccount (settled to their bank T+1).
"""
from alembic import op
import sqlalchemy as sa

revision = 'a9b8c7d6e5f4'
down_revision = '3a4b5c6d7e8f'
branch_labels = None
depends_on = None


def upgrade():
    # Add paystack_subaccount_code to storefronts
    with op.batch_alter_table('storefronts', schema=None) as batch_op:
        batch_op.add_column(
            sa.Column('paystack_subaccount_code', sa.String(length=100), nullable=True)
        )

    # Add paystack_subaccount_code to vendor_bank_accounts
    with op.batch_alter_table('vendor_bank_accounts', schema=None) as batch_op:
        batch_op.add_column(
            sa.Column('paystack_subaccount_code', sa.String(length=100), nullable=True)
        )


def downgrade():
    with op.batch_alter_table('vendor_bank_accounts', schema=None) as batch_op:
        batch_op.drop_column('paystack_subaccount_code')

    with op.batch_alter_table('storefronts', schema=None) as batch_op:
        batch_op.drop_column('paystack_subaccount_code')
