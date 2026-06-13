"""add trust tables

Revision ID: f6b7c8d9e0f1
Revises: fb755621eae3
Create Date: 2026-06-13 10:26:00.000000

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'f6b7c8d9e0f1'
down_revision = 'fb755621eae3'
branch_labels = None
depends_on = None


def upgrade():
    # 1. Create vendor_trust_profiles table
    op.create_table('vendor_trust_profiles',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('vendor_id', sa.Integer(), nullable=False),
        sa.Column('completion_score', sa.Numeric(precision=5, scale=2), nullable=False),
        sa.Column('satisfaction_score', sa.Numeric(precision=5, scale=2), nullable=False),
        sa.Column('responsiveness_score', sa.Numeric(precision=5, scale=2), nullable=False),
        sa.Column('compliance_score', sa.Numeric(precision=5, scale=2), nullable=False),
        sa.Column('community_score', sa.Numeric(precision=5, scale=2), nullable=False),
        sa.Column('total_trust_score', sa.Integer(), nullable=False),
        sa.Column('trust_tier', sa.String(length=20), nullable=False),
        sa.Column('last_recalculated', sa.DateTime(timezone=True), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(['vendor_id'], ['users.id'], ),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('vendor_id')
    )

    # 2. Create trust_score_history table
    op.create_table('trust_score_history',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('vendor_id', sa.Integer(), nullable=False),
        sa.Column('score_before', sa.Integer(), nullable=False),
        sa.Column('score_after', sa.Integer(), nullable=False),
        sa.Column('change_reason', sa.String(length=255), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(['vendor_id'], ['users.id'], ),
        sa.PrimaryKeyConstraint('id')
    )


def downgrade():
    op.drop_table('trust_score_history')
    op.drop_table('vendor_trust_profiles')
