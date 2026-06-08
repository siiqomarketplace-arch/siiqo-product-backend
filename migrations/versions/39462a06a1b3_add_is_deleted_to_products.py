""" Add is_deleted to products

Revision ID: 39462a06a1b3
Revises: 35fa50cb6baa
Create Date: 2026-06-07 07:16:22.275762

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = '39462a06a1b3'
down_revision = '35fa50cb6baa'
branch_labels = None
depends_on = None


def upgrade():
    bind = op.get_bind()
    dialect = bind.dialect.name

    if dialect == 'postgresql':
        bind.execute(sa.text(
            "ALTER TABLE products ADD COLUMN IF NOT EXISTS is_deleted BOOLEAN"
        ))
        bind.execute(sa.text(
            "UPDATE products SET is_deleted = false WHERE is_deleted IS NULL"
        ))
    else:
        import sqlalchemy as sa
        inspector = sa.inspect(bind)
        columns = [c['name'] for c in inspector.get_columns('products')]
        if 'is_deleted' not in columns:
            with op.batch_alter_table('products', schema=None) as batch_op:
                batch_op.add_column(sa.Column('is_deleted', sa.Boolean(), nullable=True))
            op.execute("UPDATE products SET is_deleted = false WHERE is_deleted IS NULL")


def downgrade():
    with op.batch_alter_table('products', schema=None) as batch_op:
        batch_op.drop_column('is_deleted')
