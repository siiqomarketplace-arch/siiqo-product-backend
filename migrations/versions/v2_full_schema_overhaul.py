"""v2 full schema overhaul

Revision ID: v2_full_schema_overhaul
Revises: aa3e829401e7
Create Date: 2026-05-11

Changes:
- User: added is_active, city, state; timezone-aware datetimes
- Storefront: added meta_title, meta_description, is_live property; timezone-aware datetimes
- EscrowTransaction: added fee_amount, payscrow_ref, payment_link; auto-generates transaction_number
- Article: changed author_id FK from users → admin_users; added excerpt, meta fields
- Review: new model (replaces Notification hack)
- PartnerStaff: new model for partner team management
- Ledger: added balance_after column
- Campaign: added subject, body, sent_count columns
- Coupon: fixed field names (vendor_id, discount_value, usage_limit)
- All datetimes: timezone-aware
"""
from alembic import op
import sqlalchemy as sa


revision = 'v2_full_schema_overhaul'
down_revision = 'aa3e829401e7'
branch_labels = None
depends_on = None


def upgrade():
    # --- users ---
    with op.batch_alter_table('users', schema=None) as batch_op:
        batch_op.add_column(sa.Column('is_active', sa.Boolean(), nullable=True, server_default='1'))
        batch_op.add_column(sa.Column('city', sa.String(100), nullable=True))
        batch_op.add_column(sa.Column('state', sa.String(100), nullable=True))

    # --- storefronts ---
    with op.batch_alter_table('storefronts', schema=None) as batch_op:
        batch_op.add_column(sa.Column('meta_title', sa.String(255), nullable=True))
        batch_op.add_column(sa.Column('meta_description', sa.Text(), nullable=True))

    # --- escrow_transactions ---
    with op.batch_alter_table('escrow_transactions', schema=None) as batch_op:
        batch_op.add_column(sa.Column('fee_amount', sa.Numeric(10, 2), nullable=True))
        batch_op.add_column(sa.Column('payscrow_ref', sa.String(255), nullable=True))
        batch_op.add_column(sa.Column('payment_link', sa.String(500), nullable=True))
        # Make transaction_number nullable temporarily for migration
        batch_op.alter_column('transaction_number', existing_type=sa.String(100), nullable=True)

    # --- articles ---
    with op.batch_alter_table('articles', schema=None) as batch_op:
        batch_op.add_column(sa.Column('admin_author_id', sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column('excerpt', sa.String(500), nullable=True))
        batch_op.add_column(sa.Column('meta_title', sa.String(255), nullable=True))
        batch_op.add_column(sa.Column('meta_description', sa.String(500), nullable=True))
        batch_op.create_foreign_key('fk_articles_admin_author', 'admin_users', ['admin_author_id'], ['id'])

    # --- ledgers ---
    with op.batch_alter_table('ledgers', schema=None) as batch_op:
        batch_op.add_column(sa.Column('balance_after', sa.Numeric(10, 2), nullable=True))

    # --- campaigns ---
    with op.batch_alter_table('campaigns', schema=None) as batch_op:
        batch_op.add_column(sa.Column('subject', sa.String(255), nullable=True))
        batch_op.add_column(sa.Column('body', sa.Text(), nullable=True))
        batch_op.add_column(sa.Column('sent_count', sa.Integer(), nullable=True, server_default='0'))

    # --- partner_applications ---
    with op.batch_alter_table('partner_applications', schema=None) as batch_op:
        batch_op.add_column(sa.Column('state_of_operation', sa.String(100), nullable=True))

    # --- reviews (new table) ---
    op.create_table(
        'reviews',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('order_id', sa.Integer(), sa.ForeignKey('orders.id'), nullable=False),
        sa.Column('buyer_id', sa.Integer(), sa.ForeignKey('users.id'), nullable=False),
        sa.Column('vendor_id', sa.Integer(), sa.ForeignKey('users.id'), nullable=False),
        sa.Column('product_id', sa.Integer(), sa.ForeignKey('products.id'), nullable=True),
        sa.Column('vendor_rating', sa.Integer(), nullable=False),
        sa.Column('product_rating', sa.Integer(), nullable=True),
        sa.Column('review_text', sa.Text(), nullable=True),
        sa.Column('is_approved', sa.Boolean(), nullable=True, server_default='1'),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint('id'),
    )

    # --- partner_staff (new table) ---
    op.create_table(
        'partner_staff',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('partner_id', sa.Integer(), sa.ForeignKey('users.id'), nullable=False),
        sa.Column('staff_name', sa.String(100), nullable=False),
        sa.Column('staff_phone', sa.String(20), nullable=False),
        sa.Column('staff_email', sa.String(120), nullable=True),
        sa.Column('staff_role', sa.String(50), nullable=True, server_default='RIDER'),
        sa.Column('is_active', sa.Boolean(), nullable=True, server_default='1'),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint('id'),
    )


def downgrade():
    op.drop_table('partner_staff')
    op.drop_table('reviews')

    with op.batch_alter_table('partner_applications', schema=None) as batch_op:
        batch_op.drop_column('state_of_operation')

    with op.batch_alter_table('campaigns', schema=None) as batch_op:
        batch_op.drop_column('sent_count')
        batch_op.drop_column('body')
        batch_op.drop_column('subject')

    with op.batch_alter_table('ledgers', schema=None) as batch_op:
        batch_op.drop_column('balance_after')

    with op.batch_alter_table('articles', schema=None) as batch_op:
        batch_op.drop_constraint('fk_articles_admin_author', type_='foreignkey')
        batch_op.drop_column('meta_description')
        batch_op.drop_column('meta_title')
        batch_op.drop_column('excerpt')
        batch_op.drop_column('admin_author_id')

    with op.batch_alter_table('escrow_transactions', schema=None) as batch_op:
        batch_op.drop_column('payment_link')
        batch_op.drop_column('payscrow_ref')
        batch_op.drop_column('fee_amount')

    with op.batch_alter_table('storefronts', schema=None) as batch_op:
        batch_op.drop_column('meta_description')
        batch_op.drop_column('meta_title')

    with op.batch_alter_table('users', schema=None) as batch_op:
        batch_op.drop_column('state')
        batch_op.drop_column('city')
        batch_op.drop_column('is_active')
