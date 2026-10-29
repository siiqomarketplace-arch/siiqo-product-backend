"""The trust score is earned by using Siiqo. Paying (plan or old badge),
typed NIN/CAC/bank numbers, and uploaded logos or banners add nothing."""
from datetime import datetime, timedelta, timezone

from app.extensions import db
from app.models.withdrawal import VendorBankAccount
from app.services.trust import calculate_compliance_score


def test_payment_typed_numbers_and_uploads_add_nothing(make_vendor):
    vendor, store = make_vendor()
    base = calculate_compliance_score(vendor.id)
    store.is_pro_verified = True
    store.pro_verified_expires_at = datetime.now(timezone.utc) + timedelta(days=30)
    store.verification_status = "VERIFIED"
    store.cac_reg = "RC1234567"
    store.store_logo = "https://cdn.example/logo.png"
    store.banner_url = "https://cdn.example/banner.png"
    store.account_number = "0123456789"
    vendor.nin = "12345678901"
    db.session.commit()
    assert calculate_compliance_score(vendor.id) == base


def test_real_checks_and_tenure_earn_full_points(make_vendor, make_order):
    vendor, _ = make_vendor()
    vendor.is_verified = True
    vendor.created_at = datetime.now(timezone.utc) - timedelta(days=120)
    db.session.add(VendorBankAccount(vendor_id=vendor.id, bank_name="Test Bank", bank_code="000",
                                     account_number="0000000000", account_name="Test Vendor", is_verified=True))
    db.session.commit()
    assert calculate_compliance_score(vendor.id) == 150.0  # no completed order yet

    order = make_order(vendor, total=1000)
    order.status = "COMPLETED"
    db.session.commit()
    assert calculate_compliance_score(vendor.id) == 200.0


def test_new_seller_gets_no_tenure_points(make_vendor, make_order):
    vendor, _ = make_vendor()
    order = make_order(vendor, total=1000)
    order.status = "COMPLETED"
    db.session.commit()
    assert calculate_compliance_score(vendor.id) == 0.0
