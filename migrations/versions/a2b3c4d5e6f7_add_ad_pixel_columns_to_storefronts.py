"""Add ad tracking pixel columns to storefronts table.

Revision ID: a2b3c4d5e6f7
Revises: ff00aa11bb22
Create Date: 2026-09-09 23:11:00.000000

Adds three nullable VARCHAR(50) columns to `storefronts`:
  - meta_pixel_id      : Meta / Facebook Pixel ID
  - tiktok_pixel_id    : TikTok Pixel ID
  - ga4_measurement_id : Google Analytics 4 Measurement ID (G-XXXXXXXXXX)

All columns default to NULL. Existing rows are unaffected.
Safe to run on live production with zero downtime.
"""
from alembic import op
import sqlalchemy as sa

revision = 'a2b3c4d5e6f7'
down_revision = 'ff00aa11bb22'
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table('storefronts', schema=None) as batch_op:
        batch_op.add_column(sa.Column('meta_pixel_id',      sa.String(50), nullable=True))
        batch_op.add_column(sa.Column('tiktok_pixel_id',    sa.String(50), nullable=True))
        batch_op.add_column(sa.Column('ga4_measurement_id', sa.String(50), nullable=True))


def downgrade():
    with op.batch_alter_table('storefronts', schema=None) as batch_op:
        batch_op.drop_column('ga4_measurement_id')
        batch_op.drop_column('tiktok_pixel_id')
        batch_op.drop_column('meta_pixel_id')
