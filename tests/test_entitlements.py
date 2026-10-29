"""Verified Pro step 1: one feature catalogue decides what a seller can use.
A cancelled plan keeps its features until the paid period ends; the old paid
badge keeps invoicing; Lifetime is gone."""
from datetime import datetime, timedelta, timezone

import pytest

from app.extensions import db
from app.models.admin import SubscriptionPlan, VendorSubscription
from app.services import entitlements as ent


def _subscribe(vendor, status="ACTIVE", days=30, plan_name="PRO_MONTHLY"):
    plan = SubscriptionPlan.query.filter_by(name=plan_name).first()
    if not plan:
        plan = SubscriptionPlan(name=plan_name, price_ngn=3000, features={}, is_active=True)
        db.session.add(plan)
        db.session.flush()
    now = datetime.now(timezone.utc)
    db.session.add(VendorSubscription(vendor_id=vendor.id, plan_id=plan.id, status=status,
                                      start_date=now, end_date=now + timedelta(days=days)))
    db.session.commit()


def test_free_seller(make_vendor):
    vendor, _ = make_vendor()
    e = ent.entitlements(vendor.id)
    assert e["plan"]["name"] == "FREE"
    assert not any(e["features"].values())
    assert e["limits"] == {"products": 10, "invoices_per_month": 6}


@pytest.mark.parametrize("status", ["ACTIVE", "CANCELLED_PENDING_EXPIRY"])
def test_paid_seller_gets_pro_tools(make_vendor, status):
    vendor, _ = make_vendor()
    _subscribe(vendor, status=status)
    for key in ("unlimited_products", "crm", "invoices_unlimited", "pod"):
        assert ent.has_feature(vendor.id, key), key
    # trust-sensitive: needs a passed identity check too
    assert not ent.has_feature(vendor.id, "verified_badge")
    assert ent.entitlements(vendor.id)["limits"] == {"products": None, "invoices_per_month": None}


def test_expired_or_cancelled_plan_is_free(make_vendor):
    vendor, _ = make_vendor()
    _subscribe(vendor, days=-1)
    _subscribe(vendor, status="CANCELLED")
    assert not ent.plan_active(vendor.id)


def test_legacy_badge_keeps_invoicing_only(make_vendor):
    vendor, store = make_vendor()
    store.is_pro_verified = True
    store.pro_verified_expires_at = datetime.now(timezone.utc) + timedelta(days=30)
    db.session.commit()
    assert ent.has_feature(vendor.id, "invoices_unlimited")
    assert not ent.has_feature(vendor.id, "crm")


def test_unknown_feature_is_an_error(make_vendor):
    vendor, _ = make_vendor()
    with pytest.raises(KeyError):
        ent.has_feature(vendor.id, "no_such_feature")


def test_entitlements_endpoint(client, make_vendor, auth_headers):
    vendor, _ = make_vendor()
    _subscribe(vendor)
    data = client.get("/api/vendor/entitlements", headers=auth_headers(vendor)).get_json()
    assert data["plan"]["name"] == "PRO_MONTHLY"
    assert data["features"]["crm"] is True


def test_cancelled_but_paid_seller_keeps_pod(make_vendor):
    """Before: POD ignored CANCELLED_PENDING_EXPIRY, so a seller lost it early."""
    vendor, store = make_vendor()
    _subscribe(vendor, status="CANCELLED_PENDING_EXPIRY")
    assert ent.has_feature(vendor.id, "pod")


def test_lifetime_is_gone(client, make_vendor, auth_headers, monkeypatch):
    from app.routes import bridge
    monkeypatch.setattr(bridge, "PAYSTACK_SECRET_KEY", "sk_test_not_a_real_key")
    vendor, _ = make_vendor()
    res = client.post("/api/payments/initiate-pro-subscription", json={"billing_cycle": "lifetime"},
                      headers=auth_headers(vendor))
    assert res.status_code == 400


def test_plan_list_shows_real_fee_and_no_lifetime(client):
    plans = client.get("/api/payments/plans").get_json()["plans"]
    assert all(p["name"] != "LIFETIME" for p in plans)
    assert {p["transaction_fee_pct"] for p in plans} == {3.0}
