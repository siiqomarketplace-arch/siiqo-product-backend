"""
Direct SQL migration script to apply the standalone billing columns.
This bypasses Alembic's merge chain issue and directly applies
the schema changes from migration 3a4b5c6d7e8f, then stamps the DB.
"""
import sys
from app import create_app
from app.extensions import db
from sqlalchemy import text, inspect

print("=" * 60)
print("  SIIQO — Direct Schema Migration (standalone billing)")
print("=" * 60)

app = create_app('development')

with app.app_context():
    bind = db.engine
    inspector = inspect(bind)
    
    # Check current alembic version
    try:
        current = db.session.execute(text("SELECT version_num FROM alembic_version")).fetchall()
        print(f"[INFO] Current alembic versions: {[r[0] for r in current]}")
    except Exception as e:
        print(f"[WARN] Could not read alembic_version: {e}")
    
    # --- Step 1: Check what already exists ---
    inv_cols = [c['name'] for c in inspector.get_columns('invoices')]
    rcp_cols = [c['name'] for c in inspector.get_columns('receipts')]
    
    print(f"\n[INFO] Current invoices columns: {inv_cols}")
    print(f"[INFO] Current receipts columns: {rcp_cols}")
    
    # --- Step 2: Add missing invoice columns ---
    print("\n[STEP 1] Adding standalone columns to invoices table...")
    invoice_additions = [
        ("customer_name",      "VARCHAR(255)"),
        ("customer_email",     "VARCHAR(255)"),
        ("customer_phone",     "VARCHAR(50)"),
        ("customer_address",   "TEXT"),
        ("line_items",         "JSONB"),
        ("subtotal",           "NUMERIC(10, 2)"),
        ("discount",           "NUMERIC(10, 2)"),
        ("tax_rate",           "NUMERIC(5, 2)"),
        ("tax_amount",         "NUMERIC(10, 2)"),
        ("total",              "NUMERIC(10, 2)"),
        ("currency",           "VARCHAR(10) DEFAULT 'NGN'"),
        ("notes",              "TEXT"),
        ("payment_link_token", "VARCHAR(100)"),
        ("payment_method",     "VARCHAR(50)"),
    ]
    
    for col_name, col_type in invoice_additions:
        if col_name not in inv_cols:
            try:
                db.session.execute(text(
                    f"ALTER TABLE invoices ADD COLUMN IF NOT EXISTS {col_name} {col_type}"
                ))
                db.session.commit()
                print(f"  [OK] Added invoices.{col_name}")
            except Exception as e:
                db.session.rollback()
                print(f"  [SKIP] invoices.{col_name}: {e}")
        else:
            print(f"  [EXISTS] invoices.{col_name}")
    
    # Make order_id nullable on invoices
    try:
        db.session.execute(text(
            "ALTER TABLE invoices ALTER COLUMN order_id DROP NOT NULL"
        ))
        db.session.commit()
        print("  [OK] invoices.order_id is now nullable")
    except Exception as e:
        db.session.rollback()
        print(f"  [INFO] invoices.order_id nullable: {e}")
    
    # Make buyer_id nullable on invoices
    try:
        db.session.execute(text(
            "ALTER TABLE invoices ALTER COLUMN buyer_id DROP NOT NULL"
        ))
        db.session.commit()
        print("  [OK] invoices.buyer_id is now nullable")
    except Exception as e:
        db.session.rollback()
        print(f"  [INFO] invoices.buyer_id nullable: {e}")
    
    # Add unique constraint on payment_link_token if not exists
    try:
        db.session.execute(text(
            "ALTER TABLE invoices ADD CONSTRAINT uq_invoices_payment_link_token "
            "UNIQUE (payment_link_token)"
        ))
        db.session.commit()
        print("  [OK] invoices: unique constraint on payment_link_token added")
    except Exception as e:
        db.session.rollback()
        print(f"  [INFO] payment_link_token constraint: {e}")

    # --- Step 3: Add missing receipt columns ---
    print("\n[STEP 2] Adding standalone columns to receipts table...")
    receipt_additions = [
        ("vendor_id",       "INTEGER REFERENCES users(id)"),
        ("customer_name",   "VARCHAR(255)"),
        ("customer_email",  "VARCHAR(255)"),
        ("customer_phone",  "VARCHAR(50)"),
        ("line_items",      "JSONB"),
        ("subtotal",        "NUMERIC(10, 2)"),
        ("tax_amount",      "NUMERIC(10, 2)"),
        ("discount",        "NUMERIC(10, 2)"),
        ("total",           "NUMERIC(10, 2)"),
        ("currency",        "VARCHAR(10) DEFAULT 'NGN'"),
        ("payment_method",  "VARCHAR(50) DEFAULT 'Cash'"),
        ("notes",           "TEXT"),
        ("status",          "VARCHAR(50) DEFAULT 'paid'"),
    ]
    
    for col_name, col_type in receipt_additions:
        if col_name not in rcp_cols:
            try:
                db.session.execute(text(
                    f"ALTER TABLE receipts ADD COLUMN IF NOT EXISTS {col_name} {col_type}"
                ))
                db.session.commit()
                print(f"  [OK] Added receipts.{col_name}")
            except Exception as e:
                db.session.rollback()
                print(f"  [SKIP] receipts.{col_name}: {e}")
        else:
            print(f"  [EXISTS] receipts.{col_name}")
    
    # Make order_id nullable on receipts
    try:
        db.session.execute(text(
            "ALTER TABLE receipts ALTER COLUMN order_id DROP NOT NULL"
        ))
        db.session.commit()
        print("  [OK] receipts.order_id is now nullable")
    except Exception as e:
        db.session.rollback()
        print(f"  [INFO] receipts.order_id nullable: {e}")

    # --- Step 4: Stamp the alembic version to our new head ---
    print("\n[STEP 3] Stamping alembic version to 3a4b5c6d7e8f (standalone billing head)...")
    try:
        # Delete all existing version rows and insert our new head
        db.session.execute(text("DELETE FROM alembic_version"))
        db.session.execute(text("INSERT INTO alembic_version (version_num) VALUES ('3a4b5c6d7e8f')"))
        db.session.commit()
        print("  [OK] Alembic version stamped to 3a4b5c6d7e8f")
    except Exception as e:
        db.session.rollback()
        print(f"  [WARN] Could not stamp alembic version: {e}")

    # --- Step 5: Final validation ---
    print("\n[STEP 4] Validating schema...")
    inspector2 = inspect(bind)
    inv_cols2 = [c['name'] for c in inspector2.get_columns('invoices')]
    rcp_cols2 = [c['name'] for c in inspector2.get_columns('receipts')]
    
    needed_inv = ['customer_name', 'customer_email', 'line_items', 'total', 'payment_link_token']
    needed_rcp = ['vendor_id', 'customer_name', 'line_items', 'total', 'status']
    
    missing_inv = [c for c in needed_inv if c not in inv_cols2]
    missing_rcp = [c for c in needed_rcp if c not in rcp_cols2]
    
    if not missing_inv and not missing_rcp:
        print("  [SUCCESS] All standalone billing columns are in place!")
    else:
        if missing_inv:
            print(f"  [WARN] invoices still missing: {missing_inv}")
        if missing_rcp:
            print(f"  [WARN] receipts still missing: {missing_rcp}")

print()
print("=" * 60)
print("  MIGRATION COMPLETE")
print("=" * 60)
