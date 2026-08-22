"""Add blog_authors table and author_id to articles

Revision ID: d1e2f3a4b5c6
Revises: a9b8c7d6e5f4
Create Date: 2026-08-22 14:00:00.000000

"""
from alembic import op
import sqlalchemy as sa
import uuid

# revision identifiers, used by Alembic.
revision = 'd1e2f3a4b5c6'
down_revision = 'a9b8c7d6e5f4'
branch_labels = None
depends_on = None


def upgrade():
    # ── 1. Create blog_authors table ─────────────────────────────────────────
    op.create_table(
        'blog_authors',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('name', sa.String(100), nullable=False),
        sa.Column('slug', sa.String(120), nullable=False, unique=True),
        sa.Column('title', sa.String(150), nullable=True),
        sa.Column('bio', sa.Text(), nullable=True),
        sa.Column('avatar', sa.String(255), nullable=True),
        sa.Column('twitter_handle', sa.String(100), nullable=True),
        sa.Column('linkedin_url', sa.String(255), nullable=True),
        sa.Column('is_active', sa.Boolean(), nullable=False, server_default='true'),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()')),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()')),
    )
    op.create_index('ix_blog_authors_slug', 'blog_authors', ['slug'], unique=True)

    # ── 2. Add author_id FK column to articles ────────────────────────────────
    with op.batch_alter_table('articles', schema=None) as batch_op:
        batch_op.add_column(sa.Column('author_id', sa.Integer(), nullable=True))
        batch_op.create_foreign_key(
            'fk_articles_author_id_blog_authors',
            'blog_authors',
            ['author_id'], ['id'],
            ondelete='SET NULL'
        )

    # ── 3. Seed default authors ───────────────────────────────────────────────
    bind = op.get_bind()

    # Siiqo Editorial Team
    existing = bind.execute(sa.text(
        "SELECT id FROM blog_authors WHERE slug = 'siiqo-editorial-team'"
    )).fetchone()
    if not existing:
        bind.execute(sa.text("""
            INSERT INTO blog_authors (name, slug, title, bio, avatar, is_active)
            VALUES (
                'Siiqo Editorial Team', 'siiqo-editorial-team',
                'Official Siiqo Content Team',
                'In-house content team at Siiqo covering e-commerce, vendor growth, logistics and SME insights across West Africa.',
                'https://siiqo.com/images/siiqo.png', TRUE
            )
        """))

    # Okereke
    existing_ok = bind.execute(sa.text(
        "SELECT id FROM blog_authors WHERE slug = 'okereke'"
    )).fetchone()
    if not existing_ok:
        bind.execute(sa.text("""
            INSERT INTO blog_authors (name, slug, title, bio, avatar, is_active)
            VALUES (
                'Okereke', 'okereke', 'Siiqo Contributor',
                'A contributor to the Siiqo blog covering commerce and entrepreneurship.',
                'https://siiqo.com/images/siiqo.png', TRUE
            )
        """))

    # ── 4. Link existing articles where author_name = 'Okereke' ──────────────
    okereke_row = bind.execute(sa.text(
        "SELECT id FROM blog_authors WHERE slug = 'okereke'"
    )).fetchone()
    if okereke_row:
        bind.execute(sa.text("""
            UPDATE articles
            SET author_id = :aid
            WHERE lower(author_name) = 'okereke' AND author_id IS NULL
        """), {"aid": okereke_row[0]})

    # ── 5. Ensure official Siiqo Editorial system user exists ─────────────────
    editorial_user = bind.execute(sa.text(
        "SELECT id FROM users WHERE email = 'editorial@siiqo.com'"
    )).fetchone()

    if not editorial_user:
        import hashlib, secrets
        salt = secrets.token_hex(16)
        h = hashlib.pbkdf2_hmac('sha256', str(uuid.uuid4()).encode(), salt.encode(), 260000)
        pw = f"pbkdf2:sha256:260000${salt}${h.hex()}"
        bind.execute(sa.text("""
            INSERT INTO users
                (email, password_hash, first_name, last_name, role,
                 profile_pic, is_verified, is_active)
            VALUES
                ('editorial@siiqo.com', :pw, 'Siiqo Editorial', '', 'ADMIN',
                 'https://siiqo.com/images/siiqo.png', TRUE, TRUE)
        """), {"pw": pw})

    # ── 6. Remove ghost @siiqo.com ADMIN users created by the old auto-post logic
    PROTECTED = ('admin@siiqo.com', 'gov@siiqo.com', 'editorial@siiqo.com')
    placeholders = ', '.join([f"'{e}'" for e in PROTECTED])

    ghost_users = bind.execute(sa.text(f"""
        SELECT id, email FROM users
        WHERE email LIKE '%@siiqo.com'
          AND role = 'ADMIN'
          AND email NOT IN ({placeholders})
    """)).fetchall()

    if ghost_users:
        editorial_row = bind.execute(sa.text(
            "SELECT id FROM users WHERE email = 'editorial@siiqo.com'"
        )).fetchone()

        for u in ghost_users:
            uid = u[0]
            if editorial_row:
                bind.execute(sa.text(
                    "UPDATE posts SET user_id = :eid WHERE user_id = :uid"
                ), {"eid": editorial_row[0], "uid": uid})
            else:
                bind.execute(sa.text("DELETE FROM posts WHERE user_id = :uid"), {"uid": uid})
            bind.execute(sa.text("DELETE FROM users WHERE id = :uid"), {"uid": uid})


def downgrade():
    with op.batch_alter_table('articles', schema=None) as batch_op:
        batch_op.drop_constraint('fk_articles_author_id_blog_authors', type_='foreignkey')
        batch_op.drop_column('author_id')

    op.drop_index('ix_blog_authors_slug', table_name='blog_authors')
    op.drop_table('blog_authors')
