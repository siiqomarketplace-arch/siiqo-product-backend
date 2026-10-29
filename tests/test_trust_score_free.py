"""The trust score is earned by using Siiqo: paying for a plan or the old
badge, or typing a NIN/CAC number, adds nothing."""
from datetime import datetime, timedelta, timezone

from app.extensions import db
from app.models.withdrawal import VendorBankAccount
from app.services.trust import calculate_compliance_score


def test_payment_and_typed_numbers_add_nothing(make_vendor):
    vendor, store = make_vendor()
    base = calculate_compliance_score(vendor.id)
    store.is_pro_verified = True
    store.pro_verified_expires_at = datetime.now(timezone.utc) + timedelta(days=30)
    store.verification_status = "VERIFIED"
    store.cac_reg = "RC1234567"
    vendor.nin = "12345678901"
    db.session.commit()
    assert calculate_compliance_score(vendor.id) == base


def test_free_profile_steps_earn_full_points(make_vendor):
    vendor, store = make_vendor()
    vendor.is_verified = True
    store.store_logo = "https://cdn.example/logo.png"
    db.session.add(VendorBankAccount(vendor_id=vendor.id, bank_name="Test Bank", bank_code="000",
                                     account_number="0000000000", account_name="Test Vendor", is_verified=True))
    db.session.commit()
    assert calculate_compliance_score(vendor.id) == 200.0
