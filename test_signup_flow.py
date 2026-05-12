# -*- coding: utf-8 -*-
"""
Full signup flow test — hits the live Flask server at 127.0.0.1:5000
"""
import sys
import os
import requests
import json

# Force UTF-8 output on Windows
sys.stdout.reconfigure(encoding='utf-8', errors='replace')

BASE = "http://127.0.0.1:5000/api"
EMAIL = "stillwalker689@gmail.com"
PASSWORD = "123456789Still"

def show(label, res):
    print("\n" + "="*60)
    print("  " + label)
    print("  Status  : " + str(res.status_code))
    try:
        body = res.json()
        print("  Response: " + json.dumps(body, indent=4, ensure_ascii=True))
    except Exception:
        print("  Response: " + res.text[:400])
    print("="*60)
    return res

# ── Step 0: Health check ─────────────────────────────────────
print("\nStep 0: Checking backend is reachable at " + BASE)
try:
    r = requests.get("http://127.0.0.1:5000/health", timeout=5)
    print("  BACKEND IS UP: " + str(r.json()))
except Exception as e:
    print("  BACKEND IS DOWN: " + str(e))
    print("\n  >>> Start the backend first:")
    print("      cd 'Siiqo backend'")
    print("      venv\\Scripts\\activate")
    print("      flask run")
    sys.exit(1)

# ── Step 1: Register ─────────────────────────────────────────
print("\nStep 1: Register " + EMAIL)
r = requests.post(f"{BASE}/auth/register", json={
    "email": EMAIL,
    "password": PASSWORD,
    "first_name": "Still",
    "last_name": "Walker",
})
show("REGISTER", r)

if r.status_code == 409:
    print("  User already exists — resending OTP...")
    r2 = requests.post(f"{BASE}/auth/resend-otp", json={"email": EMAIL})
    show("RESEND OTP", r2)
elif r.status_code not in (200, 201):
    print("  REGISTRATION FAILED. Stopping.")
    sys.exit(1)

# ── Step 2: Read OTP from DB ─────────────────────────────────
print("\nStep 2: Reading OTP from database...")
os.environ.setdefault("FLASK_ENV", "development")

from app import create_app
from app.models.user import User

app = create_app()
otp = None
with app.app_context():
    user = User.query.filter_by(email=EMAIL).first()
    if user:
        print("  User found in DB:")
        print("    ID        : " + str(user.id))
        print("    Email     : " + str(user.email))
        print("    Role      : " + str(user.role))
        print("    Verified  : " + str(user.is_verified))
        print("    OTP       : " + str(user.reset_otp) + "  <-- USE THIS")
        print("    OTP Expiry: " + str(user.otp_expiry))
        otp = user.reset_otp
    else:
        print("  ERROR: User NOT found in DB after registration!")
        sys.exit(1)

if not otp:
    print("  ERROR: No OTP stored. Email may have failed silently.")
    print("  Check MAIL_SERVER settings in .env")
    sys.exit(1)

# ── Step 3: Verify email ─────────────────────────────────────
print("\nStep 3: Verifying email with OTP: " + str(otp))
r = requests.post(f"{BASE}/auth/verify-email", json={
    "email": EMAIL,
    "otp": otp,
})
show("VERIFY EMAIL", r)

if r.status_code != 200:
    print("  VERIFICATION FAILED. Stopping.")
    sys.exit(1)

token = r.json().get("access_token")
print("  Token received: " + ("YES" if token else "NO"))

# ── Step 4: Login ────────────────────────────────────────────
print("\nStep 4: Login...")
r = requests.post(f"{BASE}/auth/login", json={
    "email": EMAIL,
    "password": PASSWORD,
})
show("LOGIN", r)

if r.status_code != 200:
    print("  LOGIN FAILED.")
    sys.exit(1)

token = r.json().get("access_token") or r.json().get("token")
print("  Token: " + str(token)[:40] + "...")

# ── Step 5: Get profile ──────────────────────────────────────
print("\nStep 5: Fetching profile...")
r = requests.get(f"{BASE}/auth/profile", headers={"Authorization": f"Bearer {token}"})
show("PROFILE", r)

# ── Step 6: Test unverified login block ──────────────────────
print("\nStep 6: Testing unverified login is blocked...")
r = requests.post(f"{BASE}/auth/register", json={
    "email": "unverified_temp@siiqo.com",
    "password": "TestPass123",
    "first_name": "Temp",
    "last_name": "User",
})
if r.status_code == 201:
    r2 = requests.post(f"{BASE}/auth/login", json={
        "email": "unverified_temp@siiqo.com",
        "password": "TestPass123",
    })
    show("UNVERIFIED LOGIN (expect 403)", r2)
    if r2.status_code == 403:
        print("  PASS: Unverified login correctly blocked!")
    else:
        print("  FAIL: Unverified login was NOT blocked!")
    # Clean up temp user
    with app.app_context():
        from app.extensions import db
        u = User.query.filter_by(email="unverified_temp@siiqo.com").first()
        if u:
            db.session.delete(u)
            db.session.commit()
            print("  Cleaned up temp user.")

# ── Step 7: Wrong password ───────────────────────────────────
print("\nStep 7: Testing wrong password (expect 401)...")
r = requests.post(f"{BASE}/auth/login", json={
    "email": EMAIL,
    "password": "WrongPassword999",
})
show("WRONG PASSWORD (expect 401)", r)
if r.status_code == 401:
    print("  PASS: Wrong password correctly rejected!")
else:
    print("  FAIL: Wrong password was accepted!")

print("\n" + "="*60)
print("  ALL TESTS COMPLETE")
print("="*60)
print("\n  Test account ready:")
print("  Email    : " + EMAIL)
print("  Password : " + PASSWORD)
print("  Status   : Verified")
print("  Role     : BUYER")
print("\n  Login at: http://localhost:3000/auth/login")
