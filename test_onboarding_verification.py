import sys
import os
import io

# Force local sqlite to prevent database lock/network latency
os.environ['DATABASE_URL'] = 'sqlite:///siiqo.db'

# Add backend directory to python path
sys.path.append(os.path.dirname(os.path.abspath(__file__)))

from app import create_app, db
from app.models.user import User, Storefront, UserRole
from flask_jwt_extended import create_access_token

print("=" * 60)
print("  SIIQO VENDOR ONBOARDING & VERIFICATION FLOW TEST")
print("=" * 60)

app = create_app('development')
app.config['TESTING'] = True
client = app.test_client()

with app.app_context():
    # Setup test database
    db.create_all()

    # 1. Clean up existing test users and storefronts
    from app.models.admin import AdminUser
    from app.models.trust import TrustScoreHistory, VendorTrustProfile
    test_vendor = User.query.filter_by(email="test_onboard_vendor@siiqo.com").first()
    if test_vendor:
        Storefront.query.filter_by(vendor_id=test_vendor.id).delete()
        TrustScoreHistory.query.filter_by(vendor_id=test_vendor.id).delete()
        VendorTrustProfile.query.filter_by(vendor_id=test_vendor.id).delete()
        db.session.delete(test_vendor)
    AdminUser.query.filter_by(email="test_admin@siiqo.com").delete()
    db.session.commit()

    # 2. Seed Admin User
    admin = AdminUser(
        name="Super Admin",
        email="test_admin@siiqo.com",
        role="SUPERADMIN",
        is_active=True
    )
    admin.set_password("AdminPassword123!")
    db.session.add(admin)

    # 3. Seed Vendor User
    vendor = User(
        email="test_onboard_vendor@siiqo.com",
        first_name="Sam",
        last_name="Vendor",
        role=UserRole.BUYER, # Starts as buyer before onboarding
        is_verified=True,    # Must be email-verified to onboard
        is_active=True
    )
    vendor.set_password("VendorPassword123!")
    db.session.add(vendor)
    db.session.commit()

    print("[OK] Test users seeded successfully.")

    # 4. Generate JWT Tokens via Login API
    print("[INFO] Logging in vendor...")
    vendor_login_resp = client.post('/api/auth/login', json={
        "email": "test_onboard_vendor@siiqo.com",
        "password": "VendorPassword123!"
    })
    assert vendor_login_resp.status_code == 200, f"Vendor login failed: {vendor_login_resp.text}"
    vendor_token = vendor_login_resp.get_json()['access_token']

    print("[INFO] Logging in admin...")
    admin_login_resp = client.post('/api/admin/login', json={
        "email": "test_admin@siiqo.com",
        "password": "AdminPassword123!"
    })
    assert admin_login_resp.status_code == 200, f"Admin login failed: {admin_login_resp.text}"
    admin_token = admin_login_resp.get_json()['access_token']

    vendor_headers = {"Authorization": f"Bearer {vendor_token}"}
    admin_headers = {"Authorization": f"Bearer {admin_token}"}

    # 5. Onboard Vendor with BUSINESS account and CAC file
    print("[INFO] Onboarding vendor with CAC business registration...")
    onboard_payload = {
        "business_name": "Premium Auto Parts",
        "description": "Dealers in quality engines",
        "phone": "08098765432",
        "account_type": "BUSINESS",
        "cac_reg": "RC-998877",
        # Mock file uploads
        "logo": (io.BytesIO(b"logo_content"), "logo.png"),
        "cac_document": (io.BytesIO(b"cac_doc_content"), "cac_cert.png")
    }

    resp = client.post('/api/vendor/onboard', data=onboard_payload, headers=vendor_headers)
    print("Onboarding Status Code:", resp.status_code)
    assert resp.status_code == 201, f"Onboarding failed: {resp.text}"
    print("[OK] Onboarding completed successfully.")

    # Check database state
    db.session.expire_all()
    sf = Storefront.query.filter_by(vendor_id=vendor.id).first()
    assert sf is not None, "Storefront record not created!"
    print(f"Storefront created: name={sf.store_name}, type={sf.account_type}, cac={sf.cac_reg}")
    print(f"Document URL: {sf.cac_document_url}")
    print(f"Initial Verification Status: {sf.verification_status}")
    
    assert sf.account_type == "BUSINESS", "Account type mismatch"
    assert sf.cac_reg == "RC-998877", "CAC number mismatch"
    assert sf.verification_status == "PENDING_VERIFY_SUB", "Should be PENDING_VERIFY_SUB"
    assert sf.cac_document_url is not None, "Document URL should not be null"

    # 6. Admin queries the Verification Queue
    print("[INFO] Admin retrieving pending verification queue...")
    resp = client.get('/api/admin/storefronts?status=PENDING_VERIFY_SUB', headers=admin_headers)
    print("Admin Queue Status Code:", resp.status_code)
    assert resp.status_code == 200, f"Failed to retrieve queue: {resp.text}"
    
    queue_data = resp.get_json()
    pending_list = queue_data.get("storefronts", [])
    print(f"Found {len(pending_list)} pending verifications.")
    assert len(pending_list) >= 1, "Vendor should appear in the queue list!"
    
    vendor_entry = [s for s in pending_list if s["id"] == sf.id][0]
    print(f"Found vendor entry: {vendor_entry['business_name']} (NIN: {vendor_entry['nin']}, CAC: {vendor_entry['cac_reg']})")
    assert vendor_entry["verification_status"] == "PENDING_VERIFY_SUB"

    # 7. Admin APPROVES the Vendor
    print("[INFO] Admin approving vendor verification...")
    approve_resp = client.patch(f'/api/admin/users/{vendor.id}/status', json={"status": "approved"}, headers=admin_headers)
    print("Admin Approval Status Code:", approve_resp.status_code)
    assert approve_resp.status_code == 200, f"Approval failed: {approve_resp.text}"

    # Verify approved state
    db.session.expire_all()
    assert sf.verification_status == "VERIFIED", "Status should be VERIFIED"
    assert sf.is_verified is True, "is_verified should be True"
    assert vendor.is_verified is True, "user.is_verified should be True"
    print(f"[OK] Vendor approved. New status: {sf.verification_status}, is_verified={sf.is_verified}")

    # 8. Admin REJECTS the Vendor
    print("[INFO] Admin rejecting vendor verification...")
    reject_resp = client.patch(f'/api/admin/users/{vendor.id}/status', json={"status": "rejected"}, headers=admin_headers)
    print("Admin Rejection Status Code:", reject_resp.status_code)
    assert reject_resp.status_code == 200, f"Rejection failed: {reject_resp.text}"

    # Verify rejected state
    db.session.expire_all()
    assert sf.verification_status == "REJECTED", "Status should be REJECTED"
    assert sf.is_verified is False, "is_verified should be False"
    print(f"[OK] Vendor rejected. New status: {sf.verification_status}, is_verified={sf.is_verified}")

print("=" * 60)
print("  TEST COMPLETED SUCCESSFULLY!")
print("=" * 60)
