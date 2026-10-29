"""M8: a sponsorship payment features the product and never grants Pro; only
real subscription payments create a subscription; renewals extend by the
plan's own interval."""
import hashlib
import hmac
import json
from datetime import datetime, timedelta, timezone

import pytest

from app.extensions import db
from app.models.admin import SponsoredListing, SubscriptionPlan, VendorSubscription
from app.routes import bridge

TEST_KEY = "sk_test_not_a_real_key"


@pytest.fixture(autouse=True)
def _signing_key(monkeypatch):
    monkeypatch.setattr(bridge, "PAYSTACK_SECRET_KEY", TEST_KEY)


def _post(client, event):
    body = json.dumps(event).encode()
    sig = hmac.new(TEST_KEY.encode(), body, hashlib.sha512).hexdigest()
    return client.post("/api/payments/webhook", data=body,
                       headers={"Content-Type": "application/json", "x-paystack-signature": sig})


def _charge(email, reference, metadata=None, amount=200000, plan=None):
    data = {"reference": reference, "status": "success", "amount": amount,
            "customer": {"email": email}, "metadata": metadata or {}}
    if plan is not None:
        data["plan"] = plan
    return {"event": "charge.success", "data": data}


def _aware(dt):
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt


def test_webhook_url(client):
    # guard against the test silently hitting a 404 route
    assert _post(client, {"event": "ping"}).status_code == 200


def test_sponsorship_features_product_and_grants_no_pro(client, make_vendor, make_product):
    vendor, store = make_vendor()
    product = make_product(store)
    ref = f"SPONSOR-{product.id}-ABCD1234"
    event = _charge(vendor.email, ref, {"type": "sponsored_listing",
                                        "vendor_id": vendor.id, "product_id": product.id})

    assert _post(client, event).status_code == 200
    assert _post(client, event).status_code == 200  # Paystack retry

    listings = SponsoredListing.query.filter_by(product_id=product.id).all()
    assert len(listings) == 1
    assert float(listings[0].amount_paid) == 2000.0
    assert _aware(listings[0].end_date) > datetime.now(timezone.utc) + timedelta(days=6)
    assert VendorSubscription.query.filter_by(vendor_id=vendor.id).count() == 0


def test_sponsorship_for_someone_elses_product_is_ignored(client, make_vendor, make_product):
    vendor, _ = make_vendor()
    _, other_store = make_vendor()
    product = make_product(other_store)
    event = _charge(vendor.email, f"SPONSOR-{product.id}-X",
                    {"type": "sponsored_listing", "vendor_id": vendor.id, "product_id": product.id})
    assert _post(client, event).status_code == 200
    assert SponsoredListing.query.count() == 0


def test_unknown_charge_grants_no_pro(client, make_vendor):
    vendor, _ = make_vendor()
    assert _post(client, _charge(vendor.email, "RANDOM-REF-1")).status_code == 200
    assert VendorSubscription.query.filter_by(vendor_id=vendor.id).count() == 0


def test_subscription_payment_still_creates_subscription(client, make_vendor):
    vendor, _ = make_vendor()
    event = _charge(vendor.email, f"SUB-PRO_ANNUAL-{vendor.id}-ABCD",
                    {"plan_name": "PRO_ANNUAL", "billing_cycle": "annual"}, amount=2400000)
    assert _post(client, event).status_code == 200
    sub = VendorSubscription.query.filter_by(vendor_id=vendor.id, status="ACTIVE").one()
    assert sub.plan.name == "PRO_ANNUAL"
    assert _aware(sub.end_date) > datetime.now(timezone.utc) + timedelta(days=360)


@pytest.mark.parametrize("plan_name,min_days,max_days", [("PRO_ANNUAL", 360, 370), ("PRO_MONTHLY", 27, 32)])
def test_renewal_extends_by_plan_interval(client, make_vendor, plan_name, min_days, max_days):
    vendor, _ = make_vendor()
    plan = SubscriptionPlan(name=plan_name, price_ngn=3000, features={}, is_active=True)
    db.session.add(plan)
    db.session.flush()
    now = datetime.now(timezone.utc)
    db.session.add(VendorSubscription(vendor_id=vendor.id, plan_id=plan.id, status="ACTIVE",
                                      start_date=now, end_date=now))
    db.session.commit()

    event = {"event": "invoice.payment_success",
             "data": {"status": "success", "customer": {"email": vendor.email}}}
    assert _post(client, event).status_code == 200

    db.session.expire_all()
    sub = VendorSubscription.query.filter_by(vendor_id=vendor.id).one()
    days = (_aware(sub.end_date) - now).days
    assert min_days <= days <= max_days
