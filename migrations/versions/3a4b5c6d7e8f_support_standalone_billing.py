"""Support standalone billing for invoices and receipts

Revision ID: 3a4b5c6d7e8f
Revises: ff00aa11bb22
Create Date: 2026-06-02 14:30:00.000000
"""
from alembic import op
import sqlalchemy as sa

revision = '3a4b5c6d7e8f'
down_revision = 'ff00aa11bb22'
branch_labels = None
depends_on = None

def upgrade():
    # 1. Alter Invoices Table
    with op.batch_alter_table('invoices', schema=None) as batch_op:
        # Make existing columns nullable
        batch_op.alter_column('order_id', existing_type=sa.Integer(), nullable=True)
        batch_op.alter_column('buyer_id', existing_type=sa.Integer(), nullable=True)
        
        # Add new standalone columns
        batch_op.add_column(sa.Column('customer_name', sa.String(length=255), nullable=True))
        batch_op.add_column(sa.Column('customer_email', sa.String(length=255), nullable=True))
        batch_op.add_column(sa.Column('customer_phone', sa.String(length=50), nullable=True))
        batch_op.add_column(sa.Column('customer_address', sa.Text(), nullable=True))
        batch_op.add_column(sa.Column('line_items', sa.JSON(), nullable=True))
        batch_op.add_column(sa.Column('subtotal', sa.Numeric(precision=10, scale=2), nullable=True))
        batch_op.add_column(sa.Column('discount', sa.Numeric(precision=10, scale=2), nullable=True))
        batch_op.add_column(sa.Column('tax_rate', sa.Numeric(precision=5, scale=2), nullable=True))
        batch_op.add_column(sa.Column('tax_amount', sa.Numeric(precision=10, scale=2), nullable=True))
        batch_op.add_column(sa.Column('total', sa.Numeric(precision=10, scale=2), nullable=True))
        batch_op.add_column(sa.Column('currency', sa.String(length=10), server_default='NGN', nullable=True))
        batch_op.add_column(sa.Column('notes', sa.Text(), nullable=True))
        batch_op.add_column(sa.Column('payment_link_token', sa.String(length=100), nullable=True))
        batch_op.add_column(sa.Column('payment_method', sa.String(length=50), nullable=True))
        
        # Create unique constraint on payment_link_token
        batch_op.create_unique_constraint('uq_invoices_payment_link_token', ['payment_link_token'])

    # 2. Alter Receipts Table
    with op.batch_alter_table('receipts', schema=None) as batch_op:
        # Make existing columns nullable
        batch_op.alter_column('order_id', existing_type=sa.Integer(), nullable=True)
        
        # Add new standalone columns
        batch_op.add_column(sa.Column('vendor_id', sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column('customer_name', sa.String(length=255), nullable=True))
        batch_op.add_column(sa.Column('customer_email', sa.String(length=255), nullable=True))
        batch_op.add_column(sa.Column('customer_phone', sa.String(length=50), nullable=True))
        batch_op.add_column(sa.Column('line_items', sa.JSON(), nullable=True))
        batch_op.add_column(sa.Column('subtotal', sa.Numeric(precision=10, scale=2), nullable=True))
        batch_op.add_column(sa.Column('tax_amount', sa.Numeric(precision=10, scale=2), nullable=True))
        batch_op.add_column(sa.Column('discount', sa.Numeric(precision=10, scale=2), nullable=True))
        batch_op.add_column(sa.Column('total', sa.Numeric(precision=10, scale=2), nullable=True))
        batch_op.add_column(sa.Column('currency', sa.String(length=10), server_default='NGN', nullable=True))
        batch_op.add_column(sa.Column('payment_method', sa.String(length=50), server_default='Cash', nullable=True))
        batch_op.add_column(sa.Column('notes', sa.Text(), nullable=True))
        batch_op.add_column(sa.Column('status', sa.String(length=50), server_default='paid', nullable=True))
        
        # Add ForeignKey constraint for vendor_id on receipts table
        batch_op.create_foreign_key('fk_receipts_vendor_id_users', 'users', ['vendor_id'], ['id'])

def downgrade():
    # 1. Revert Invoices Table
    with op.batch_alter_table('invoices', schema=None) as batch_op:
        batch_op.drop_constraint('uq_invoices_payment_link_token', type_='unique')
        batch_op.drop_column('payment_method')
        batch_op.drop_column('payment_link_token')
        batch_op.drop_column('notes')
        batch_op.drop_column('currency')
        batch_op.drop_column('total')
        batch_op.drop_column('tax_amount')
        batch_op.drop_column('tax_rate')
        batch_op.drop_column('discount')
        batch_op.drop_column('subtotal')
        batch_op.drop_column('line_items')
        batch_op.drop_column('customer_address')
        batch_op.drop_column('customer_phone')
        batch_op.drop_column('customer_email')
        batch_op.drop_column('customer_name')
        batch_op.alter_column('buyer_id', existing_type=sa.Integer(), nullable=False)
        batch_op.alter_column('order_id', existing_type=sa.Integer(), nullable=False)

    # 2. Revert Receipts Table
    with op.batch_alter_table('receipts', schema=None) as batch_op:
        batch_op.drop_constraint('fk_receipts_vendor_id_users', type_='foreignkey')
        batch_op.drop_column('status')
        batch_op.drop_column('notes')
        batch_op.drop_column('payment_method')
        batch_op.drop_column('currency')
        batch_op.drop_column('total')
        batch_op.drop_column('discount')
        batch_op.drop_column('tax_amount')
        batch_op.drop_column('subtotal')
        batch_op.drop_column('line_items')
        batch_op.drop_column('customer_phone')
        batch_op.drop_column('customer_email')
        batch_op.drop_column('customer_name')
        batch_op.drop_column('vendor_id')
        batch_op.alter_column('order_id', existing_type=sa.Integer(), nullable=False)
