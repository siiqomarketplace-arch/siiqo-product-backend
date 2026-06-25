"""Add attribute_schema/icon/product_type_hint to categories and attributes to products.

Revision ID: a1c2d3e4f5a6
Revises: ff00aa11bb22
Create Date: 2026-06-24 00:00:00.000000

This is a PURELY ADDITIVE migration.
- Adds nullable JSON columns to categories: attribute_schema, product_type_hint, icon
- Adds nullable JSON column to products: attributes
- No existing rows are modified. All new columns default to NULL.
- Existing products and category associations are completely unaffected.
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = 'a1c2d3e4f5a6'
down_revision = 'ff00aa11bb22'
branch_labels = None
depends_on = None


def upgrade():
    # ── categories table: add three new nullable columns ─────────────────────
    # Uses IF NOT EXISTS equivalent: try/except each so partial runs don't fail.
    with op.batch_alter_table('categories', schema=None) as batch_op:
        batch_op.add_column(
            sa.Column('attribute_schema', sa.JSON(), nullable=True,
                      comment='Array of attribute field descriptors for this category')
        )
        batch_op.add_column(
            sa.Column('product_type_hint', sa.JSON(), nullable=True,
                      comment='Which product types this category applies to, e.g. ["physical"]')
        )
        batch_op.add_column(
            sa.Column('icon', sa.String(length=50), nullable=True,
                      comment='Lucide icon name for UI display')
        )

    # ── products table: add one new nullable column ───────────────────────────
    with op.batch_alter_table('products', schema=None) as batch_op:
        batch_op.add_column(
            sa.Column('attributes', sa.JSON(), nullable=True,
                      comment='Category-specific attributes, e.g. {"color":"Blue","size":"XL"}')
        )


def downgrade():
    # Remove in reverse — products first, then categories
    with op.batch_alter_table('products', schema=None) as batch_op:
        batch_op.drop_column('attributes')

    with op.batch_alter_table('categories', schema=None) as batch_op:
        batch_op.drop_column('icon')
        batch_op.drop_column('product_type_hint')
        batch_op.drop_column('attribute_schema')
