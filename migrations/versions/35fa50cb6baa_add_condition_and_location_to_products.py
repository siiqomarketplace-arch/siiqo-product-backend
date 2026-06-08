""" Add condition and location to products

Revision ID: 35fa50cb6baa
Revises: a7b4b4e06d2b
Create Date: 2026-06-07 06:59:25.997344

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = '35fa50cb6baa'
down_revision = 'a7b4b4e06d2b'
branch_labels = None
depends_on = None


def upgrade():
    bind = op.get_bind()
    dialect = bind.dialect.name

    if dialect == 'postgresql':
        # Orders table — tracking_number
        bind.execute(sa.text(
            "ALTER TABLE orders ADD COLUMN IF NOT EXISTS tracking_number VARCHAR(100)"
        ))
        # Products table — condition, location, lat/lng
        bind.execute(sa.text(
            "ALTER TABLE products ADD COLUMN IF NOT EXISTS condition VARCHAR(50)"
        ))
        bind.execute(sa.text(
            "ALTER TABLE products ADD COLUMN IF NOT EXISTS location VARCHAR(255)"
        ))
        bind.execute(sa.text(
            "ALTER TABLE products ADD COLUMN IF NOT EXISTS latitude FLOAT"
        ))
        bind.execute(sa.text(
            "ALTER TABLE products ADD COLUMN IF NOT EXISTS longitude FLOAT"
        ))
        # Reviews unique constraint — use DO $$ to guard against duplicate constraint
        bind.execute(sa.text("""
            DO $$
            BEGIN
                IF NOT EXISTS (
                    SELECT 1 FROM pg_constraint
                    WHERE conname = 'uq_review_order_buyer'
                ) THEN
                    ALTER TABLE reviews
                    ADD CONSTRAINT uq_review_order_buyer UNIQUE (order_id, buyer_id);
                END IF;
            END
            $$;
        """))
    else:
        inspector = sa.inspect(bind)

        order_cols = [c['name'] for c in inspector.get_columns('orders')]
        product_cols = [c['name'] for c in inspector.get_columns('products')]

        with op.batch_alter_table('orders', schema=None) as batch_op:
            if 'tracking_number' not in order_cols:
                batch_op.add_column(sa.Column('tracking_number', sa.String(length=100), nullable=True))

        with op.batch_alter_table('products', schema=None) as batch_op:
            if 'condition' not in product_cols:
                batch_op.add_column(sa.Column('condition', sa.String(length=50), nullable=True))
            if 'location' not in product_cols:
                batch_op.add_column(sa.Column('location', sa.String(length=255), nullable=True))
            if 'latitude' not in product_cols:
                batch_op.add_column(sa.Column('latitude', sa.Float(), nullable=True))
            if 'longitude' not in product_cols:
                batch_op.add_column(sa.Column('longitude', sa.Float(), nullable=True))

        with op.batch_alter_table('reviews', schema=None) as batch_op:
            batch_op.create_unique_constraint('uq_review_order_buyer', ['order_id', 'buyer_id'])


def downgrade():
    with op.batch_alter_table('reviews', schema=None) as batch_op:
        batch_op.drop_constraint('uq_review_order_buyer', type_='unique')

    with op.batch_alter_table('products', schema=None) as batch_op:
        batch_op.drop_column('longitude')
        batch_op.drop_column('latitude')
        batch_op.drop_column('location')
        batch_op.drop_column('condition')

    with op.batch_alter_table('orders', schema=None) as batch_op:
        batch_op.drop_column('tracking_number')
