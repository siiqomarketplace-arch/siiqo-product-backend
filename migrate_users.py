"""
migrate_users.py
Safely migrates all users from old local SQLite to new AWS RDS PostgreSQL.
- Passwords are preserved (users can log in as normal OR use Forgot Password)
- All image fields (profile_pic, store_logo etc.) are set to NULL
  since the hacker deleted all files. Users can re-upload via their dashboard.
"""
import sqlite3
import os
from datetime import datetime
from app import create_app
from app.extensions import db
from app.models.user import User

SQLITE_PATH = os.path.join(os.path.dirname(__file__), 'instance', 'siiqo.db')

app = create_app('development')

with app.app_context():
    # Step 1: Read ALL users from old SQLite
    conn = sqlite3.connect(SQLITE_PATH)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM users")
    old_users = cursor.fetchall()
    conn.close()

    total = len(old_users)
    print(f"\nFound {total} users in old SQLite database.")
    print("Migrating to AWS RDS (bulk mode for speed)...\n")

    migrated = 0
    skipped  = 0
    errors   = 0
    batch    = []

    columns = old_users[0].keys() if old_users else []

    for row in old_users:
        try:
            email = row['email']

            # Skip if already in new DB
            if User.query.filter_by(email=email).first():
                skipped += 1
                continue

            user = User(
                email         = email,
                password_hash = row['password_hash'],
                first_name    = row['first_name']   if 'first_name'   in columns else None,
                last_name     = row['last_name']    if 'last_name'    in columns else None,
                phone         = row['phone']        if 'phone'        in columns else None,
                # ⚠️ Images set to NULL — hacker deleted all files.
                # Users must re-upload via their dashboard / settings page.
                profile_pic   = None,
                role          = row['role']         if 'role'         in columns else 'BUYER',
                is_verified   = bool(row['is_verified']) if 'is_verified' in columns else False,
                reset_otp     = None,   # Clear any old OTPs for security
                otp_expiry    = None,
                referral_code = row['referral_code'] if 'referral_code' in columns else None,
                points_balance= row['points_balance'] if 'points_balance' in columns else 0.00,
                created_at    = datetime.fromisoformat(str(row['created_at'])) if row['created_at'] else datetime.utcnow(),
                updated_at    = datetime.utcnow(),
            )
            batch.append(user)
            migrated += 1

        except Exception as e:
            print(f"  [ERROR] Prep failed for {row['email']}: {e}")
            errors += 1

    # Bulk insert all prepared users in one transaction
    try:
        if batch:
            db.session.bulk_save_objects(batch)
            db.session.commit()
            print(f"  [SUCCESS] Bulk inserted {len(batch)} users into AWS RDS!")
    except Exception as e:
        db.session.rollback()
        print(f"  [FATAL] Bulk insert failed: {e}")
        print("  Falling back to one-by-one insert...")
        errors_fallback = 0
        for user in batch:
            try:
                db.session.add(user)
                db.session.commit()
            except Exception as e2:
                db.session.rollback()
                print(f"  [ERROR] {user.email}: {e2}")
                errors_fallback += 1
        print(f"  Fallback complete. Errors: {errors_fallback}")

    print(f"\n{'=' * 55}")
    print(f"  ✅  Migration Complete!")
    print(f"  Migrated  : {migrated} users  → AWS RDS (images = null)")
    print(f"  Skipped   : {skipped}  (already existed in new DB)")
    print(f"  Errors    : {errors}")
    print(f"{'=' * 55}")
    print(f"\n  Users can now:")
    print(f"  1. Log in with their old password as normal.")
    print(f"  2. OR use Forgot Password to reset via email OTP.")
    print(f"  3. Re-upload their profile picture from their dashboard.")
    print(f"  4. Re-upload storefront logos, banners, and product images.\n")
