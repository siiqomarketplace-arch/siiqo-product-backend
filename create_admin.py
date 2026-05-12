"""
create_admin.py
Creates a SUPERADMIN account for the Siiqo admin portal.
Run once: venv\Scripts\python create_admin.py
"""
from app import create_app
from app.extensions import db
from app.models.admin import AdminUser
from werkzeug.security import generate_password_hash

app = create_app('development')

with app.app_context():
    # --- CONFIGURE YOUR ADMIN CREDENTIALS HERE ---
    ADMIN_NAME     = "Siiqo Admin"
    ADMIN_EMAIL    = "admin@siiqo.com"
    ADMIN_PASSWORD = "SiiqoAdmin2026!"   # Change this to something secure
    ADMIN_ROLE     = "SUPERADMIN"
    # -----------------------------------------------

    existing = AdminUser.query.filter_by(email=ADMIN_EMAIL).first()
    if existing:
        print(f"\n[INFO] Admin already exists: {ADMIN_EMAIL}")
        print(f"       Role: {existing.role}")
        print("       Use Forgot Password flow if you need to reset.\n")
    else:
        admin = AdminUser(
            name          = ADMIN_NAME,
            email         = ADMIN_EMAIL,
            password_hash = generate_password_hash(ADMIN_PASSWORD),
            role          = ADMIN_ROLE,
            is_active     = True
        )
        db.session.add(admin)
        db.session.commit()
        print(f"\n{'=' * 50}")
        print(f"  SUPERADMIN CREATED SUCCESSFULLY!")
        print(f"  Email    : {ADMIN_EMAIL}")
        print(f"  Password : {ADMIN_PASSWORD}")
        print(f"  Role     : {ADMIN_ROLE}")
        print(f"  Login at : http://localhost:3000/admin/login")
        print(f"{'=' * 50}\n")
        print("  IMPORTANT: Change the password after first login!")
