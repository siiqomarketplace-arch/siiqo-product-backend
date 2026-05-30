"""Add subscription_plans, vendor_subscriptions, sponsored_listings, favourites tables

Revision ID: a1b2c3d4e5f6
Revises: f110e3b88494
Create Date: 2026-05-30 12:00:00.000000

These tables were originally created directly on the production RDS instance
and never had a migration. This migration creates them if they don't exist,
so that fresh deployments (staging, new RDS) work correctly.
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


# revision identifiers, used by Alembic.
revision = 'a1b2c3d4e5f6'
down_revision = '20eee4b60e25'
branch_labels = None
depends_on = None


def upgrade():
    conn = op.get_bind()
    inspector = sa.inspect(conn)
    existing_tables = inspector.get_table_names()

    # --- subscription_plans ---
    if 'subscription_plans' not in existing_tables:
        op.create_table(
            'subscription_plans',
            sa.Column('id', sa.Integer(), nullable=False),
            sa.Column('name', sa.String(length=50), nullable=False),
            sa.Column('price_ngn', sa.Numeric(10, 2), nullable=False),
            sa.Column('features', sa.JSON(), nullable=True),
            sa.Column('is_active', sa.Boolean(), nullable=True, server_default='true'),
            sa.PrimaryKeyConstraint('id'),
            sa.UniqueConstraint('name'),
        )

    # --- vendor_subscriptions ---
    if 'vendor_subscriptions' not in existing_tables:
        op.create_table(
            'vendor_subscriptions',
            sa.Column('id', sa.Integer(), nullable=False),
            sa.Column('vendor_id', sa.Integer(), nullable=False),
            sa.Column('plan_id', sa.Integer(), nullable=False),
            sa.Column('status', sa.String(length=20), nullable=True, server_default='ACTIVE'),
            sa.Column('start_date', sa.DateTime(timezone=True), nullable=True),
            sa.Column('end_date', sa.DateTime(timezone=True), nullable=False),
            sa.ForeignKeyConstraint(['plan_id'], ['subscription_plans.id']),
            sa.ForeignKeyConstraint(['vendor_id'], ['users.id']),
            sa.PrimaryKeyConstraint('id'),
        )
        op.create_index(
            'ix_vendor_subscriptions_vendor_id',
            'vendor_subscriptions',
            ['vendor_id'],
        )

    # --- sponsored_listings ---
    if 'sponsored_listings' not in existing_tables:
        op.create_table(
            'sponsored_listings',
            sa.Column('id', sa.Integer(), nullable=False),
            sa.Column('product_id', sa.Integer(), nullable=False),
            sa.Column('vendor_id', sa.Integer(), nullable=False),
            sa.Column('amount_paid', sa.Numeric(10, 2), nullable=False),
            sa.Column('impressions', sa.Integer(), nullable=True, server_default='0'),
            sa.Column('clicks', sa.Integer(), nullable=True, server_default='0'),
            sa.Column('start_date', sa.DateTime(timezone=True), nullable=True),
            sa.Column('end_date', sa.DateTime(timezone=True), nullable=False),
            sa.Column('is_active', sa.Boolean(), nullable=True, server_default='true'),
            sa.ForeignKeyConstraint(['product_id'], ['products.id']),
            sa.ForeignKeyConstraint(['vendor_id'], ['users.id']),
            sa.PrimaryKeyConstraint('id'),
        )

    # --- favourites ---
    if 'favourites' not in existing_tables:
        op.create_table(
            'favourites',
            sa.Column('id', sa.Integer(), nullable=False),
            sa.Column('user_id', sa.Integer(), nullable=False),
            sa.Column('product_id', sa.Integer(), nullable=True),
            sa.Column('storefront_id', sa.Integer(), nullable=True),
            sa.Column('created_at', sa.DateTime(timezone=True), nullable=True),
            sa.ForeignKeyConstraint(['product_id'], ['products.id']),
            sa.ForeignKeyConstraint(['storefront_id'], ['storefronts.id']),
            sa.ForeignKeyConstraint(['user_id'], ['users.id']),
            sa.PrimaryKeyConstraint('id'),
        )


def downgrade():
    conn = op.get_bind()
    inspector = sa.inspect(conn)
    existing_tables = inspector.get_table_names()

    for tbl in ['favourites', 'sponsored_listings', 'vendor_subscriptions', 'subscription_plans']:
        if tbl in existing_tables:
            op.drop_table(tbl)
