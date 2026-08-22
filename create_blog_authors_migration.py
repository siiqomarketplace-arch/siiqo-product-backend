#!/usr/bin/env python3
"""
Migration: Create blog_authors table + author_id on articles + seed defaults + cleanup ghost users.
Runs directly against the database using DATABASE_URL — no Flask app context needed.
Usage: python create_blog_authors_migration.py
"""

import os
import sys
import re
import uuid

# ── Load .env ─────────────────────────────────────────────────────────────────
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    env_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), '.env')
    if os.path.exists(env_path):
        with open(env_path, 'r', encoding='utf-8') as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith('#') and '=' in line:
                    k, v = line.split('=', 1)
                    os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))

# ── SQLAlchemy ────────────────────────────────────────────────────────────────
try:
    from sqlalchemy import create_engine, text
except ImportError:
    print("❌ sqlalchemy not installed. Run: pip install sqlalchemy psycopg2-binary")
    sys.exit(1)

database_url = os.environ.get('DATABASE_URL')
if not database_url:
    print("❌ DATABASE_URL not set. Add it to your .env file.")
    sys.exit(1)

# Werkzeug may not be available in the runner env — hash manually if needed
try:
    from werkzeug.security import generate_password_hash
    def _make_pw():
        return generate_password_hash(str(uuid.uuid4()))
except ImportError:
    import hashlib, secrets
    def _make_pw():
        salt = secrets.token_hex(16)
        h = hashlib.pbkdf2_hmac('sha256', str(uuid.uuid4()).encode(), salt.encode(), 260000)
        return f"pbkdf2:sha256:260000${salt}${h.hex()}"

print("=" * 60)
print("BlogAuthor Migration")
print("=" * 60)

engine = create_engine(database_url)

with engine.connect() as conn:

    # ── 1. Create blog_authors table ──────────────────────────────────────────
    conn.execute(text("""
        CREATE TABLE IF NOT EXISTS blog_authors (
            id              SERIAL PRIMARY KEY,
            name            VARCHAR(100) NOT NULL,
            slug            VARCHAR(120) NOT NULL UNIQUE,
            title           VARCHAR(150),
            bio             TEXT,
            avatar          VARCHAR(255),
            twitter_handle  VARCHAR(100),
            linkedin_url    VARCHAR(255),
            is_active       BOOLEAN NOT NULL DEFAULT TRUE,
            created_at      TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
            updated_at      TIMESTAMP WITH TIME ZONE DEFAULT NOW()
        )
    """))
    conn.commit()
    print("[OK] blog_authors table ready.")

    # ── 2. Add author_id column to articles (if missing) ─────────────────────
    result = conn.execute(text("""
        SELECT column_name FROM information_schema.columns
        WHERE table_name = 'articles' AND column_name = 'author_id'
    """)).fetchone()

    if not result:
        conn.execute(text("""
            ALTER TABLE articles
            ADD COLUMN author_id INTEGER REFERENCES blog_authors(id) ON DELETE SET NULL
        """))
        conn.commit()
        print("[OK] author_id column added to articles.")
    else:
        print("[SKIP] author_id already exists on articles.")

    # ── 3. Seed: Siiqo Editorial Team ─────────────────────────────────────────
    existing = conn.execute(text(
        "SELECT id FROM blog_authors WHERE slug = 'siiqo-editorial-team'"
    )).fetchone()

    if not existing:
        conn.execute(text("""
            INSERT INTO blog_authors (name, slug, title, bio, avatar, is_active)
            VALUES (
                'Siiqo Editorial Team',
                'siiqo-editorial-team',
                'Official Siiqo Content Team',
                'In-house content team at Siiqo covering e-commerce, vendor growth, logistics and SME insights across West Africa.',
                'https://siiqo.com/images/siiqo.png',
                TRUE
            )
        """))
        conn.commit()
        print("[OK] Seeded: Siiqo Editorial Team")
    else:
        print("[SKIP] Siiqo Editorial Team already exists.")

    # ── 4. Seed: Okereke ─────────────────────────────────────────────────────
    existing_ok = conn.execute(text(
        "SELECT id FROM blog_authors WHERE slug = 'okereke'"
    )).fetchone()

    if not existing_ok:
        conn.execute(text("""
            INSERT INTO blog_authors (name, slug, title, bio, avatar, is_active)
            VALUES (
                'Okereke',
                'okereke',
                'Siiqo Contributor',
                'A contributor to the Siiqo blog covering commerce and entrepreneurship.',
                'https://siiqo.com/images/siiqo.png',
                TRUE
            )
        """))
        conn.commit()
        print("[OK] Seeded: Okereke")
    else:
        print("[SKIP] Okereke already exists in blog_authors.")

    # ── 5. Link existing articles where author_name = 'Okereke' ──────────────
    okereke_row = conn.execute(text(
        "SELECT id FROM blog_authors WHERE slug = 'okereke'"
    )).fetchone()

    if okereke_row:
        result = conn.execute(text("""
            UPDATE articles
            SET author_id = :aid
            WHERE lower(author_name) = 'okereke' AND author_id IS NULL
        """), {"aid": okereke_row[0]})
        conn.commit()
        print(f"[OK] Linked {result.rowcount} article(s) to Okereke blog_author profile.")

    # ── 6. Remove ghost @siiqo.com ADMIN users (auto-generated by old code) ──
    print("\nCleaning ghost @siiqo.com author accounts from users table...")
    PROTECTED = ('admin@siiqo.com', 'gov@siiqo.com', 'editorial@siiqo.com')
    placeholders = ', '.join([f"'{e}'" for e in PROTECTED])

    ghost_users = conn.execute(text(f"""
        SELECT id, email, first_name FROM users
        WHERE email LIKE '%@siiqo.com'
          AND role = 'ADMIN'
          AND email NOT IN ({placeholders})
    """)).fetchall()

    if ghost_users:
        # Find or note editorial user id for reassignment
        editorial_row = conn.execute(text(
            "SELECT id FROM users WHERE email = 'editorial@siiqo.com'"
        )).fetchone()

        for u in ghost_users:
            uid, email, fname = u[0], u[1], u[2]
            print(f"  Removing ghost user: {fname} ({email})")

            if editorial_row:
                conn.execute(text(
                    "UPDATE posts SET user_id = :eid WHERE user_id = :uid"
                ), {"eid": editorial_row[0], "uid": uid})
            else:
                conn.execute(text("DELETE FROM posts WHERE user_id = :uid"), {"uid": uid})

            conn.execute(text("DELETE FROM users WHERE id = :uid"), {"uid": uid})
            conn.commit()
            print(f"  [OK] Removed: {email}")
    else:
        print("  [OK] No ghost @siiqo.com users found.")

    # ── 7. Ensure official Siiqo Editorial system user exists ─────────────────
    editorial_user = conn.execute(text(
        "SELECT id FROM users WHERE email = 'editorial@siiqo.com'"
    )).fetchone()

    if not editorial_user:
        pw = _make_pw()
        conn.execute(text("""
            INSERT INTO users
                (email, password_hash, first_name, last_name, role,
                 profile_pic, is_verified, is_active)
            VALUES
                ('editorial@siiqo.com', :pw, 'Siiqo Editorial', '', 'ADMIN',
                 'https://siiqo.com/images/siiqo.png', TRUE, TRUE)
        """), {"pw": pw})
        conn.commit()
        print("[OK] Created official Siiqo Editorial system user (editorial@siiqo.com).")
    else:
        print("[SKIP] Siiqo Editorial system user already exists.")

print("\n" + "=" * 60)
print("Migration complete!")
print("=" * 60)
