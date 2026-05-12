"""v3 community and finance expansion

Revision ID: v3_community_finance
Revises: v2_full_schema_overhaul
Create Date: 2026-05-11

"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision = 'v3_community_finance'
down_revision = 'v2_full_schema_overhaul'
branch_labels = None
depends_on = None


def upgrade():
    # Create posts table
    op.create_table('posts',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('user_id', sa.Integer(), nullable=False),
        sa.Column('post_type', sa.String(length=50), nullable=True),
        sa.Column('content', sa.Text(), nullable=False),
        sa.Column('images', sa.JSON(), nullable=True),
        sa.Column('city', sa.String(length=100), nullable=True),
        sa.Column('state', sa.String(length=100), nullable=True),
        sa.Column('latitude', sa.Float(), nullable=True),
        sa.Column('longitude', sa.Float(), nullable=True),
        sa.Column('is_pinned', sa.Boolean(), nullable=True),
        sa.Column('is_featured', sa.Boolean(), nullable=True),
        sa.Column('is_active', sa.Boolean(), nullable=True),
        sa.Column('likes_count', sa.Integer(), nullable=True),
        sa.Column('comments_count', sa.Integer(), nullable=True),
        sa.Column('shares_count', sa.Integer(), nullable=True),
        sa.Column('views_count', sa.Integer(), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(['user_id'], ['users.id'], ),
        sa.PrimaryKeyConstraint('id')
    )
    op.create_index('idx_post_feed', 'posts', ['is_active', 'created_at'])
    op.create_index('idx_post_location', 'posts', ['city', 'is_active', 'created_at'])
    op.create_index('idx_post_type', 'posts', ['post_type', 'is_active', 'created_at'])
    op.create_index(op.f('ix_posts_city'), 'posts', ['city'])
    op.create_index(op.f('ix_posts_created_at'), 'posts', ['created_at'])
    op.create_index(op.f('ix_posts_is_active'), 'posts', ['is_active'])
    op.create_index(op.f('ix_posts_is_featured'), 'posts', ['is_featured'])
    op.create_index(op.f('ix_posts_is_pinned'), 'posts', ['is_pinned'])
    op.create_index(op.f('ix_posts_post_type'), 'posts', ['post_type'])
    op.create_index(op.f('ix_posts_state'), 'posts', ['state'])
    op.create_index(op.f('ix_posts_user_id'), 'posts', ['user_id'])

    # Create post_likes table
    op.create_table('post_likes',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('post_id', sa.Integer(), nullable=False),
        sa.Column('user_id', sa.Integer(), nullable=False),
        sa.Column('reaction_type', sa.String(length=20), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(['post_id'], ['posts.id'], ),
        sa.ForeignKeyConstraint(['user_id'], ['users.id'], ),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('post_id', 'user_id', name='unique_post_like')
    )
    op.create_index('idx_post_likes', 'post_likes', ['post_id', 'user_id'])
    op.create_index(op.f('ix_post_likes_post_id'), 'post_likes', ['post_id'])
    op.create_index(op.f('ix_post_likes_user_id'), 'post_likes', ['user_id'])

    # Create post_comments table
    op.create_table('post_comments',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('post_id', sa.Integer(), nullable=False),
        sa.Column('user_id', sa.Integer(), nullable=False),
        sa.Column('parent_id', sa.Integer(), nullable=True),
        sa.Column('content', sa.Text(), nullable=False),
        sa.Column('is_active', sa.Boolean(), nullable=True),
        sa.Column('likes_count', sa.Integer(), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(['parent_id'], ['post_comments.id'], ),
        sa.ForeignKeyConstraint(['post_id'], ['posts.id'], ),
        sa.ForeignKeyConstraint(['user_id'], ['users.id'], ),
        sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_post_comments_created_at'), 'post_comments', ['created_at'])
    op.create_index(op.f('ix_post_comments_parent_id'), 'post_comments', ['parent_id'])
    op.create_index(op.f('ix_post_comments_post_id'), 'post_comments', ['post_id'])
    op.create_index(op.f('ix_post_comments_user_id'), 'post_comments', ['user_id'])

    # Create follows table
    op.create_table('follows',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('follower_id', sa.Integer(), nullable=False),
        sa.Column('following_id', sa.Integer(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(['follower_id'], ['users.id'], ),
        sa.ForeignKeyConstraint(['following_id'], ['users.id'], ),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('follower_id', 'following_id', name='unique_follow')
    )
    op.create_index('idx_follower', 'follows', ['follower_id', 'created_at'])
    op.create_index('idx_following', 'follows', ['following_id', 'created_at'])
    op.create_index(op.f('ix_follows_follower_id'), 'follows', ['follower_id'])
    op.create_index(op.f('ix_follows_following_id'), 'follows', ['following_id'])

    # Create post_views table
    op.create_table('post_views',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('post_id', sa.Integer(), nullable=False),
        sa.Column('user_id', sa.Integer(), nullable=True),
        sa.Column('ip_address', sa.String(length=45), nullable=True),
        sa.Column('user_agent', sa.String(length=255), nullable=True),
        sa.Column('viewed_at', sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(['post_id'], ['posts.id'], ),
        sa.ForeignKeyConstraint(['user_id'], ['users.id'], ),
        sa.PrimaryKeyConstraint('id')
    )
    op.create_index('idx_post_view', 'post_views', ['post_id', 'user_id', 'viewed_at'])
    op.create_index(op.f('ix_post_views_post_id'), 'post_views', ['post_id'])
    op.create_index(op.f('ix_post_views_user_id'), 'post_views', ['user_id'])
    op.create_index(op.f('ix_post_views_viewed_at'), 'post_views', ['viewed_at'])

    # Create user_activities table
    op.create_table('user_activities',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('user_id', sa.Integer(), nullable=False),
        sa.Column('activity_type', sa.String(length=50), nullable=False),
        sa.Column('points_earned', sa.Integer(), nullable=True),
        sa.Column('reference_id', sa.Integer(), nullable=True),
        sa.Column('reference_type', sa.String(length=50), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(['user_id'], ['users.id'], ),
        sa.PrimaryKeyConstraint('id')
    )
    op.create_index('idx_user_activity', 'user_activities', ['user_id', 'activity_type', 'created_at'])
    op.create_index(op.f('ix_user_activities_activity_type'), 'user_activities', ['activity_type'])
    op.create_index(op.f('ix_user_activities_created_at'), 'user_activities', ['created_at'])
    op.create_index(op.f('ix_user_activities_user_id'), 'user_activities', ['user_id'])

    # Create inventory_items table
    op.create_table('inventory_items',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('vendor_id', sa.Integer(), nullable=False),
        sa.Column('product_id', sa.Integer(), nullable=True),
        sa.Column('sku', sa.String(length=100), nullable=True),
        sa.Column('barcode', sa.String(length=100), nullable=True),
        sa.Column('name', sa.String(length=255), nullable=False),
        sa.Column('description', sa.Text(), nullable=True),
        sa.Column('category', sa.String(length=100), nullable=True),
        sa.Column('quantity', sa.Integer(), nullable=True),
        sa.Column('reorder_level', sa.Integer(), nullable=True),
        sa.Column('reorder_quantity', sa.Integer(), nullable=True),
        sa.Column('cost_price', sa.Numeric(precision=10, scale=2), nullable=True),
        sa.Column('selling_price', sa.Numeric(precision=10, scale=2), nullable=True),
        sa.Column('location', sa.String(length=100), nullable=True),
        sa.Column('batch_number', sa.String(length=100), nullable=True),
        sa.Column('expiry_date', sa.DateTime(timezone=True), nullable=True),
        sa.Column('is_active', sa.Boolean(), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(['product_id'], ['products.id'], ),
        sa.ForeignKeyConstraint(['vendor_id'], ['users.id'], ),
        sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_inventory_items_barcode'), 'inventory_items', ['barcode'])
    op.create_index(op.f('ix_inventory_items_product_id'), 'inventory_items', ['product_id'])
    op.create_index(op.f('ix_inventory_items_sku'), 'inventory_items', ['sku'])
    op.create_index(op.f('ix_inventory_items_vendor_id'), 'inventory_items', ['vendor_id'])

    # Create stock_movements table
    op.create_table('stock_movements',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('inventory_item_id', sa.Integer(), nullable=False),
        sa.Column('movement_type', sa.String(length=50), nullable=False),
        sa.Column('quantity', sa.Integer(), nullable=False),
        sa.Column('quantity_before', sa.Integer(), nullable=False),
        sa.Column('quantity_after', sa.Integer(), nullable=False),
        sa.Column('reference_type', sa.String(length=50), nullable=True),
        sa.Column('reference_id', sa.Integer(), nullable=True),
        sa.Column('notes', sa.Text(), nullable=True),
        sa.Column('performed_by', sa.Integer(), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(['inventory_item_id'], ['inventory_items.id'], ),
        sa.ForeignKeyConstraint(['performed_by'], ['users.id'], ),
        sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_stock_movements_created_at'), 'stock_movements', ['created_at'])
    op.create_index(op.f('ix_stock_movements_inventory_item_id'), 'stock_movements', ['inventory_item_id'])
    op.create_index(op.f('ix_stock_movements_movement_type'), 'stock_movements', ['movement_type'])

    # Create expenses table
    op.create_table('expenses',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('vendor_id', sa.Integer(), nullable=False),
        sa.Column('expense_date', sa.DateTime(timezone=True), nullable=True),
        sa.Column('category', sa.String(length=100), nullable=False),
        sa.Column('amount', sa.Numeric(precision=10, scale=2), nullable=False),
        sa.Column('currency', sa.String(length=10), nullable=True),
        sa.Column('payment_method', sa.String(length=50), nullable=True),
        sa.Column('vendor_name', sa.String(length=255), nullable=True),
        sa.Column('description', sa.Text(), nullable=True),
        sa.Column('receipt_url', sa.String(length=255), nullable=True),
        sa.Column('is_recurring', sa.Boolean(), nullable=True),
        sa.Column('recurrence_frequency', sa.String(length=50), nullable=True),
        sa.Column('status', sa.String(length=50), nullable=True),
        sa.Column('approved_by', sa.Integer(), nullable=True),
        sa.Column('approved_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('tags', sa.JSON(), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(['approved_by'], ['users.id'], ),
        sa.ForeignKeyConstraint(['vendor_id'], ['users.id'], ),
        sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_expenses_category'), 'expenses', ['category'])
    op.create_index(op.f('ix_expenses_expense_date'), 'expenses', ['expense_date'])
    op.create_index(op.f('ix_expenses_status'), 'expenses', ['status'])
    op.create_index(op.f('ix_expenses_vendor_id'), 'expenses', ['vendor_id'])

    # Create branding_settings table
    op.create_table('branding_settings',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('vendor_id', sa.Integer(), nullable=False),
        sa.Column('logo_url', sa.String(length=255), nullable=True),
        sa.Column('primary_color', sa.String(length=7), nullable=True),
        sa.Column('secondary_color', sa.String(length=7), nullable=True),
        sa.Column('accent_color', sa.String(length=7), nullable=True),
        sa.Column('font_family', sa.String(length=100), nullable=True),
        sa.Column('invoice_prefix', sa.String(length=20), nullable=True),
        sa.Column('invoice_next_number', sa.Integer(), nullable=True),
        sa.Column('invoice_template', sa.String(length=50), nullable=True),
        sa.Column('receipt_prefix', sa.String(length=20), nullable=True),
        sa.Column('receipt_next_number', sa.Integer(), nullable=True),
        sa.Column('receipt_template', sa.String(length=50), nullable=True),
        sa.Column('business_address', sa.Text(), nullable=True),
        sa.Column('business_phone', sa.String(length=20), nullable=True),
        sa.Column('business_email', sa.String(length=255), nullable=True),
        sa.Column('business_website', sa.String(length=255), nullable=True),
        sa.Column('tax_id', sa.String(length=100), nullable=True),
        sa.Column('default_payment_terms', sa.String(length=100), nullable=True),
        sa.Column('default_due_days', sa.Integer(), nullable=True),
        sa.Column('invoice_footer', sa.Text(), nullable=True),
        sa.Column('receipt_footer', sa.Text(), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(['vendor_id'], ['users.id'], ),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('vendor_id')
    )


def downgrade():
    op.drop_table('branding_settings')
    op.drop_table('expenses')
    op.drop_table('stock_movements')
    op.drop_table('inventory_items')
    op.drop_table('user_activities')
    op.drop_table('post_views')
    op.drop_table('follows')
    op.drop_table('post_comments')
    op.drop_table('post_likes')
    op.drop_table('posts')
