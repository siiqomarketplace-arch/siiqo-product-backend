"""Verified Pro billing: new prices, one extension per renewal, grace, auto-renew
off keeps the paid period, pay-first verification, refund after 3 mismatches,
no refund for fraud, and Pay on Delivery's verification rule."""
import hashlib
import hmac
import json
import uuid
from datetime import datetime, timedelta, timezone

import pytest

from app.extensions import db
from app.models.admin import VendorSubscription
from app.models.billing import SubscriptionPayment
from app.routes import bridge
from app.services import billing
from app.services import entitlements as ent

TEST_KEY = "sk_test_not_a_real_key"


@pytest.fixture(autouse=True)
def _keys(monkeypatch):
    monkeypatch.setattr(bridge, "PAYSTACK_SECRET_KEY", TEST_KEY)
    monkeypatch.setenv("PAYSTACK_SECRET_KEY", TEST_KEY)


@pytest.fixture
def paystack_calls(monkeypatch):
    calls = []

    class _Resp:
        ok = True

        def json(self):
            return {"status": True}

    monkeypatch.setattr(billing.requests, "post", lambda url, **k: calls.append((url, k.get("json"))) or _Resp())
    return calls


def _post(client, event):
    body = json.dumps(event).encode()
    sig = hmac.new(TEST_KEY.encode(), body, hashlib.sha512).hexdigest()
    return client.post("/api/payments/webhook", data=body,
                       headers={"Content-Type": "application/json", "x-paystack-signature": sig})


def _charge(email, reference, plan_name="PRO_MONTHLY", amount=199900):
    return {"event": "charge.success", "data": {
        "reference": reference, "status": "success", "amount": amount,
        "customer": {"email": email}, "metadata": {"plan_name": plan_name}}}


def _aware(dt):
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt


def _sub(vendor):
    return VendorSubscription.query.filter_by(vendor_id=vendor.id, status="ACTIVE").one()


def test_new_prices_in_plan_list(client):
    plans = {p["name"]: p for p in client.get("/api/payments/plans").get_json()["plans"]}
    assert plans["PRO_MONTHLY"]["price_ngn"] == 1999
    assert plans["PRO_ANNUAL"]["price_ngn"] == 19990
    assert "Accept Pay on Delivery" in plans["PRO_MONTHLY"]["features"]


def test_first_payment_starts_plan_and_is_recorded_once(client, make_vendor):
    vendor, _ = make_vendor()
    event = _charge(vendor.email, f"SUB-PRO_MONTHLY-{vendor.id}-AAAA")
    _post(client, event)
    _post(client, event)  # Paystack retry
    assert SubscriptionPayment.query.filter_by(vendor_id=vendor.id).count() == 1
    assert ent.plan_active(vendor.id)


def test_renewal_reported_twice_extends_once(client, make_vendor):
    vendor, _ = make_vendor()
    _post(client, _charge(vendor.email, f"SUB-PRO_MONTHLY-{vendor.id}-AAAA"))
    first_end = _aware(_sub(vendor).end_date)

    _post(client, _charge(vendor.email, "RENEWAL-REF-1"))
    _post(client, {"event": "invoice.payment_success", "data": {
        "status": "success", "customer": {"email": vendor.email},
        "transaction": {"reference": "RENEWAL-REF-1"}}})
    db.session.expire_all()
    days = (_aware(_sub(vendor).end_date) - first_end).days
    assert 27 <= days <= 32
    assert VendorSubscription.query.filter_by(vendor_id=vendor.id).count() == 1


def test_auto_renew_off_keeps_paid_period(client, make_vendor):
    vendor, _ = make_vendor()
    _post(client, _charge(vendor.email, f"SUB-PRO_MONTHLY-{vendor.id}-AAAA"))
    _post(client, {"event": "subscription.not_renew", "data": {"customer": {"email": vendor.email}}})
    db.session.expire_all()
    assert ent.plan_active(vendor.id)
    assert VendorSubscription.query.filter_by(vendor_id=vendor.id).one().status == "CANCELLED_PENDING_EXPIRY"


def test_grace_period(make_vendor):
    vendor, _ = make_vendor()
    billing.apply_subscription_payment(vendor, "PRO_MONTHLY", reference="R1")
    sub = _sub(vendor)
    sub.end_date = datetime.now(timezone.utc) - timedelta(days=2)
    db.session.commit()
    assert ent.plan_active(vendor.id)
    assert ent.plan_summary(vendor.id)["status"] == "GRACE"
    sub.end_date = datetime.now(timezone.utc) - timedelta(days=4)
    db.session.commit()
    assert not ent.plan_active(vendor.id)


def test_pod_needs_verification_once_it_opens(make_vendor, monkeypatch):
    vendor, _ = make_vendor()
    billing.apply_subscription_payment(vendor, "PRO_MONTHLY", reference="R1")
    monkeypatch.delenv("VERIFICATION_V2_ENABLED", raising=False)
    assert ent.has_feature(vendor.id, "pod")        # before launch: paying sellers keep POD
    monkeypatch.setenv("VERIFICATION_V2_ENABLED", "true")
    assert not ent.has_feature(vendor.id, "pod")    # after launch: plan + passed check
    assert not ent.has_feature(vendor.id, "verified_badge")


def _real_provider(monkeypatch, outcome):
    """Stand in for a real (non-mock) provider returning `outcome`."""
    from app.services import verification
    from app.services.verification import base, service

    class FakeProvider:
        name = "fakeprovider"

        def start_identity(self, **k):
            return base.IdentitySession(provider_ref="ref", hosted_url="https://provider.example/x") \
                if hasattr(base, "IdentitySession") else type("S", (), {"provider_ref": "ref", "hosted_url": "u"})()

    monkeypatch.setattr(service, "get_provider", lambda: FakeProvider())
    monkeypatch.setattr(verification, "get_provider", lambda: FakeProvider(), raising=False)
    return service


def test_real_check_requires_payment(make_vendor, monkeypatch):
    service = _real_provider(monkeypatch, None)
    monkeypatch.setattr(service, "is_available", lambda user: True)
    vendor, _ = make_vendor()
    with pytest.raises(service.VerificationError) as err:
        service.start_identity(vendor, seller_type="INDIVIDUAL", nin="12345678901", consent=True)
    assert err.value.code == "PLAN_REQUIRED"


def _failed_case(vendor, status):
    from app.models.verification import VerificationCase, VerificationKind
    case = VerificationCase(user_id=vendor.id, kind=VerificationKind.IDENTITY, seller_type="INDIVIDUAL",
                            provider="fakeprovider", status=status, idempotency_key=f"k-{uuid.uuid4().hex}")
    db.session.add(case)
    db.session.commit()
    return case


def test_third_mismatch_refunds_and_ends_plan(make_vendor, paystack_calls):
    from app.models.verification import CaseStatus
    vendor, _ = make_vendor()
    billing.apply_subscription_payment(vendor, "PRO_MONTHLY", reference="PAY-1", amount_ngn=1999)
    for _ in range(2):
        _failed_case(vendor, CaseStatus.FAILED)
        assert billing.handle_identity_mismatch(vendor.id) is False
    _failed_case(vendor, CaseStatus.FAILED)
    assert billing.handle_identity_mismatch(vendor.id) is True

    assert any(url.endswith("/refund") and body == {"transaction": "PAY-1"} for url, body in paystack_calls)
    assert SubscriptionPayment.query.filter_by(reference="PAY-1").one().status == "REFUNDED"
    assert not ent.plan_active(vendor.id)


def test_fraud_is_never_refunded(make_vendor, paystack_calls):
    from app.models.verification import CaseStatus
    vendor, _ = make_vendor()
    billing.apply_subscription_payment(vendor, "PRO_MONTHLY", reference="PAY-1")
    for _ in range(3):
        _failed_case(vendor, CaseStatus.FAILED_FRAUD)
    assert billing.handle_identity_mismatch(vendor.id) is False
    assert paystack_calls == []
    assert ent.plan_active(vendor.id)


def test_verified_mark_follows_plan(make_vendor):
    from app.models.verification import VerificationStatus
    vendor, _ = make_vendor()
    now = datetime.now(timezone.utc)
    db.session.add(VerificationStatus(user_id=vendor.id, identity_verified_at=now,
                                      valid_until=now + timedelta(days=365), source_provider="fakeprovider"))
    db.session.commit()
    billing.apply_subscription_payment(vendor, "PRO_MONTHLY", reference="R1")
    assert VerificationStatus.query.filter_by(user_id=vendor.id).one().subscription_active is True

    _sub(vendor).end_date = now - timedelta(days=10)
    db.session.commit()
    billing.run_daily_billing_tasks()
    row = VerificationStatus.query.filter_by(user_id=vendor.id).one()
    assert row.subscription_active is False
    assert row.is_identity_current()  # the check itself stays valid


def test_verified_pro_notice_sent_once(make_vendor, monkeypatch):
    import app.utils.email as email
    sent = []
    monkeypatch.setattr(email, "send_siiqo_email", lambda **k: sent.append(k) or True)
    vendor, _ = make_vendor()
    billing.apply_subscription_payment(vendor, "PRO_MONTHLY", reference="R1")
    assert billing.send_verified_pro_notice() == 1
    assert billing.send_verified_pro_notice() == 0
    assert len(sent) == 1
