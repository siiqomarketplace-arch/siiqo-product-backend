"""Add article_slug_redirects table

Revision ID: e4f5a6b7c8d9
Revises: d1e2f3a4b5c6
Create Date: 2026-08-31 18:00:00.000000

"""
from alembic import op
import sqlalchemy as sa

revision = 'e4f5a6b7c8d9'
down_revision = 'd1e2f3a4b5c6'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        'article_slug_redirects',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('article_id', sa.Integer(), sa.ForeignKey('articles.id', ondelete='CASCADE'), nullable=False),
        sa.Column('old_slug', sa.String(255), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=True),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=True),
    )
    op.create_index('ix_article_slug_redirects_article_id', 'article_slug_redirects', ['article_id'])
    op.create_index('ix_article_slug_redirects_old_slug', 'article_slug_redirects', ['old_slug'], unique=True)


def downgrade():
    op.drop_index('ix_article_slug_redirects_old_slug', table_name='article_slug_redirects')
    op.drop_index('ix_article_slug_redirects_article_id', table_name='article_slug_redirects')
    op.drop_table('article_slug_redirects')
