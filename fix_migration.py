"""
fix_migration.py
Directly stamps the alembic_version table to the local head revision,
bypassing the Flask app's auto-upgrade on startup.
Run: venv\Scripts\python.exe fix_migration.py
"""
import os
from dotenv import load_dotenv
load_dotenv()

from sqlalchemy import create_engine, text

DATABASE_URL = os.environ.get("DATABASE_URL")
if not DATABASE_URL:
    raise RuntimeError("DATABASE_URL not set")

engine = create_engine(DATABASE_URL)

LOCAL_HEAD = "361cb0130ee5"

with engine.connect() as conn:
    # Check current state
    result = conn.execute(text("SELECT version_num FROM alembic_version"))
    rows = result.fetchall()
    print(f"Current alembic_version rows: {rows}")

    if rows:
        # Update existing row
        conn.execute(
            text("UPDATE alembic_version SET version_num = :v"),
            {"v": LOCAL_HEAD}
        )
        print(f"Updated alembic_version to {LOCAL_HEAD}")
    else:
        # Insert if empty
        conn.execute(
            text("INSERT INTO alembic_version (version_num) VALUES (:v)"),
            {"v": LOCAL_HEAD}
        )
        print(f"Inserted alembic_version {LOCAL_HEAD}")

    conn.commit()
    result2 = conn.execute(text("SELECT version_num FROM alembic_version"))
    print(f"New alembic_version: {result2.fetchall()}")

print("Done. DB is now stamped at local head.")
