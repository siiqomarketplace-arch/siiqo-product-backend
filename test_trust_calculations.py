import sys
import os

# Force local sqlite to prevent AWS RDS PostgreSQL timeout in local testing
os.environ['DATABASE_URL'] = 'sqlite:///siiqo.db'

print("=" * 60)
print("       SIIQO TRUST ENGINE SYSTEM CALCULATION TEST")
print("=" * 60)

# Step 1: Import app & trust service
try:
    from app import create_app
    from app.extensions import db
    from app.services.trust import recalculate_vendor_trust, get_or_create_trust_profile
    from app.models.user import User, UserRole
    from app.models.trust import VendorTrustProfile
    print("[OK] Imports loaded successfully")
except Exception as e:
    print(f"[FAIL] Imports failed: {e}")
    sys.exit(1)

# Step 2: Create Flask app
try:
    app = create_app('development')
    print("[OK] Flask app created")
except Exception as e:
    print(f"[FAIL] Flask app creation failed: {e}")
    sys.exit(1)

with app.app_context():
    # Ensure trust tables are created in the local SQLite db if missing
    db.create_all()

    # Step 3: Clean up any existing test vendor to ensure a fresh seeding test
    existing_vendor = User.query.filter_by(email="testvendor@siiqo.com").first()
    if existing_vendor:
        VendorTrustProfile.query.filter_by(vendor_id=existing_vendor.id).delete()
        from app.models.trust import TrustScoreHistory
        TrustScoreHistory.query.filter_by(vendor_id=existing_vendor.id).delete()
        from app.models.user import Storefront
        Storefront.query.filter_by(vendor_id=existing_vendor.id).delete()
        from app.models.withdrawal import VendorBankAccount
        VendorBankAccount.query.filter_by(vendor_id=existing_vendor.id).delete()
        from app.models.social import Post
        Post.query.filter_by(user_id=existing_vendor.id).delete()
        db.session.delete(existing_vendor)
        db.session.commit()
        print("[INFO] Cleaned up existing test vendor for a fresh seeding test.")

    # Find a vendor in the local DB, or create one
    vendor = User.query.filter_by(role=UserRole.VENDOR).first()
    if not vendor:
        print("[INFO] No vendor found in database. Seeding a test vendor user...")
        from werkzeug.security import generate_password_hash
        vendor = User(
            email="testvendor@siiqo.com",
            password_hash=generate_password_hash("Password123!"),
            first_name="Test",
            last_name="Vendor",
            role=UserRole.VENDOR,
            is_verified=True,
            city="Lagos",
            state="Lagos"
        )
        db.session.add(vendor)
        db.session.commit()
        print(f"[OK] Created test vendor user '{vendor.email}' with ID {vendor.id}")
        
        # Create storefront
        from app.models.user import Storefront
        sf = Storefront(
            vendor_id=vendor.id,
            store_name="Siiqo Test Store",
            store_slug="siiqo-test-store",
            store_description="A beautiful test storefront",
            city="Lagos",
            state="Lagos",
            is_verified=True,
            is_published=True,
            cac_reg="CAC-12345678",
            account_number="1234567890",
            bank_code="011"
        )
        db.session.add(sf)
        
        # Link bank account
        from app.models.withdrawal import VendorBankAccount
        bank = VendorBankAccount(
            vendor_id=vendor.id,
            bank_name="First Bank of Nigeria",
            bank_code="011",
            account_number="1234567890",
            account_name="Test Vendor",
            is_verified=True
        )
        db.session.add(bank)
        
        # Add a couple of posts for community score
        from app.models.social import Post
        post1 = Post(
            user_id=vendor.id,
            post_type="GENERAL",
            content="Scale your business using Siiqo!",
            is_active=True
        )
        db.session.add(post1)
        
        db.session.commit()
        print(f"[OK] Seeded storefront, bank account, and community posts.")

            
    print(f"[OK] Testing calculations for vendor ID {vendor.id} ({vendor.email})")

    # Step 4: Run get or create profile
    profile = get_or_create_trust_profile(vendor.id)
    if profile:
        print(f"[OK] Trust profile loaded/initialized: Score={profile.total_trust_score}, Tier={profile.trust_tier}")
    else:
        print("[FAIL] Failed to load/create trust profile.")
        sys.exit(1)

    # Step 5: Recalculate trust score
    print("[INFO] Triggering trust score recalculation...")
    recalculated = recalculate_vendor_trust(vendor.id, reason="System Verification Test")
    
    if recalculated:
        print("\n" + "-" * 50)
        print("  RECALCULATED TRUST ENGINE RESULTS:")
        print("-" * 50)
        print(f"  Vendor Email:          {vendor.email}")
        print(f"  Completion Score:      {recalculated.completion_score} / 400.00")
        print(f"  Satisfaction Score:    {recalculated.satisfaction_score} / 250.00")
        print(f"  Responsiveness Score:  {recalculated.responsiveness_score} / 150.00")
        print(f"  Compliance Score:      {recalculated.compliance_score} / 150.00")
        print(f"  Community Score:       {recalculated.community_score} / 50.00")
        print(f"  -----------------------------------------------")
        print(f"  TOTAL TRUST SCORE:     {recalculated.total_trust_score} / 1000")
        print(f"  TRUST TIER STATUS:     {recalculated.trust_tier}")
        print(f"  Last Recalculated:     {recalculated.last_recalculated}")
        print("-" * 50 + "\n")
        print("[OK] Trust Engine Calculation Test Successful!")
    else:
        print("[FAIL] Trust Engine recalculation failed.")
        sys.exit(1)

print("=" * 60)
print("  VERIFICATION COMPLETE")
print("=" * 60)
