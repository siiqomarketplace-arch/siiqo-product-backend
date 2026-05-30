"""
fix_prod_alembic.py

Run this ONCE on the server to fix the broken alembic_version chain.
It points alembic_version to our latest known-good revision: 93b4a8e2b83c
so that flask db upgrade can run cleanly on next deploy.
"""
from app import create_app
from app.extensions import db
from sqlalchemy import text

app = create_app()

with app.app_context():
    # Check current version
    result = db.session.execute(text("SELECT version_num FROM alembic_version")).fetchall()
    print(f"Current alembic_version: {result}")

    # Stamp to our latest migration
    db.session.execute(text("UPDATE alembic_version SET version_num = '93b4a8e2b83c'"))
    db.session.commit()

    result = db.session.execute(text("SELECT version_num FROM alembic_version")).fetchall()
    print(f"Updated alembic_version: {result}")

    # Verify escrow_code column exists (should have been created by migration 93b4a8e2b83c)
    try:
        db.session.execute(text("SELECT escrow_code, payscrow_transaction_id FROM escrow_transactions LIMIT 1"))
        print("escrow_code and payscrow_transaction_id columns exist OK")
    except Exception as e:
        print(f"Columns missing — need to add manually: {e}")
        db.session.execute(text("""
            ALTER TABLE escrow_transactions
            ADD COLUMN IF NOT EXISTS payscrow_transaction_id VARCHAR(100),
            ADD COLUMN IF NOT EXISTS escrow_code VARCHAR(50)
        """))
        db.session.commit()
        print("Columns added successfully")

    print("Done. alembic_version is now fixed.")
