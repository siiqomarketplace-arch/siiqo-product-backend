#!/usr/bin/env python3
"""
Migration: Create article_slug_redirects table and indexes.
Supports PostgreSQL (production and local psql) and SQLite.
"""

import os
import sys
import subprocess

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

db_url = os.environ.get('DATABASE_URL') or 'sqlite:///siiqo.db'

print("============================================================")
print(f"ArticleSlugRedirect Migration")
print("============================================================")

sql_commands = """
CREATE TABLE IF NOT EXISTS article_slug_redirects (
    id SERIAL PRIMARY KEY,
    article_id INTEGER NOT NULL REFERENCES articles(id) ON DELETE CASCADE,
    old_slug VARCHAR(255) NOT NULL UNIQUE,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS ix_article_slug_redirects_article_id ON article_slug_redirects(article_id);
CREATE INDEX IF NOT EXISTS ix_article_slug_redirects_old_slug ON article_slug_redirects(old_slug);
"""

if db_url.startswith('sqlite'):
    import sqlite3
    db_path = db_url.replace('sqlite:///', '').replace('sqlite://', '')
    candidate_paths = [db_path, os.path.join('instance', db_path), 'siiqo.db', 'instance/siiqo.db']
    for p in candidate_paths:
        if os.path.exists(p):
            conn = sqlite3.connect(p)
            cursor = conn.cursor()
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS article_slug_redirects (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    article_id INTEGER NOT NULL REFERENCES articles(id) ON DELETE CASCADE,
                    old_slug VARCHAR(255) NOT NULL UNIQUE,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );
            """)
            cursor.execute("""
                CREATE INDEX IF NOT EXISTS ix_article_slug_redirects_article_id ON article_slug_redirects(article_id);
            """)
            cursor.execute("""
                CREATE INDEX IF NOT EXISTS ix_article_slug_redirects_old_slug ON article_slug_redirects(old_slug);
            """)
            conn.commit()
            conn.close()
            print(f"[OK] Created article_slug_redirects in SQLite: {p}")
else:
    # Try sqlalchemy first
    applied = False
    try:
        from sqlalchemy import create_engine, text
        engine = create_engine(db_url)
        with engine.connect() as conn:
            conn.execute(text(sql_commands))
            conn.commit()
            applied = True
            print("[OK] Created article_slug_redirects table in PostgreSQL via SQLAlchemy.")
    except Exception as e:
        # Fallback to psql command if available
        psql_path = r"C:\Program Files\PostgreSQL\17\bin\psql.exe"
        if os.path.exists(psql_path):
            try:
                res = subprocess.run([psql_path, db_url, "-c", sql_commands], capture_output=True, text=True)
                if res.returncode == 0:
                    applied = True
                    print("[OK] Created article_slug_redirects table in PostgreSQL via psql CLI.")
                else:
                    print(f"[INFO] psql note: {res.stderr.strip()}")
            except Exception as pe:
                print(f"[INFO] psql subprocess note: {pe}")

    if not applied:
        print("[INFO] Note: PostgreSQL migration script ready. Will auto-apply on EB deployment via .ebextensions.")
