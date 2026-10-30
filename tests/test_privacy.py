"""Data-protection mechanisms: export, consents, privacy requests, retention."""
import hashlib
import json
import os
from datetime import datetime, timedelta, timezone

import pytest
from flask_jwt_extended import create_access_token

from app.extensions import db
from app.models.admin import AdminUser, SubscriptionPlan, VendorSubscription
from app.models.billing import SubscriptionPayment
from app.models.community import Review
from app.models.communication import Notification
from app.models.order import OrderItem
from app.models.passport import PassportSettings
from app.models.privacy import Consent, PrivacyRequest
from app.models.verification import VerificationCase, VerificationStatus
from app.tasks import privacy_retention


def _utcnow():
    return datetime.now(timezone.utc)


@pytest.fixture
def superadmin():
    admin = AdminUser(name="Privacy Admin", email="privacy-admin@example.com", role="SUPERADMIN")
    admin.set_password("not-a-real-password")
    db.session.add(admin)
    db.session.commit()
    return admin


def _admin_headers(admin):
    return {"Authorization": f"Bearer {create_access_token(identity=f'admin:{admin.id}')}"}


# ── Export ──────────────────────────────────────────────────────────────────

@pytest.fixture
def seller_with_data(make_vendor, make_user, make_product, make_order):
    vendor, store = make_vendor()
    vendor.nin = "12345678901"
    vendor.reset_otp = "654321"
    store.account_number = "0123456789"
    store.paystack_subaccount_code = "ACCT_internal"
    store.nin_document_url = "https://files.example.com/nin.jpg"
    product = make_product(store)

    buyer = make_user()
    sold = make_order(vendor, buyer=buyer, buyer_phone="08011112222",
                      delivery_address="12 Buyer Street", delivery_city="Lagos")
    db.session.add(OrderItem(order_id=sold.id, product_id=product.id, price_at_purchase=1000, quantity=1))
    db.session.add(Review(order_id=sold.id, buyer_id=buyer.id, vendor_id=vendor.id,
                          vendor_rating=5, review_text="Great"))

    other_vendor, _ = make_vendor()
    bought = make_order(other_vendor, buyer=vendor, buyer_phone="08099998888")
    db.session.add(Review(order_id=bought.id, buyer_id=vendor.id, vendor_id=other_vendor.id,
                          vendor_rating=4, review_text="Fine"))

    plan = SubscriptionPlan(name="PRO", price_ngn=2500)
    db.session.add(plan)
    db.session.flush()
    sub = VendorSubscription(vendor_id=vendor.id, plan_id=plan.id,
                             end_date=_utcnow() + timedelta(days=365),
                             paystack_subscription_code="SUB_secret", paystack_email_token="tok_secret")
    db.session.add(sub)
    db.session.flush()
    db.session.add(SubscriptionPayment(vendor_id=vendor.id, subscription_id=sub.id,
                                       reference="ref-1", amount_ngn=2500))
    db.session.add(VerificationCase(user_id=vendor.id, kind="IDENTITY", provider="mock",
                                    status="PASSED", idempotency_key="idem-1",
                                    id_number_hash="a" * 64, provider_ref="prov-ref-secret",
                                    verified_name="Test User"))
    db.session.add(VerificationStatus(user_id=vendor.id, seller_type="INDIVIDUAL",
                                      identity_verified_at=_utcnow()))
    db.session.add(PassportSettings(vendor_id=vendor.id, is_public=False))
    db.session.add(Notification(user_id=vendor.id, title="Hello", message="Welcome"))
    db.session.commit()
    return vendor, buyer


def test_export_contains_own_data_and_no_secrets(client, auth_headers, seller_with_data):
    vendor, buyer = seller_with_data
    res = client.get("/api/privacy/export", headers=auth_headers(vendor))
    assert res.status_code == 200
    assert "attachment" in res.headers["Content-Disposition"]
    assert res.headers["Cache-Control"] == "no-store"
    data = json.loads(res.data)

    assert data["account"]["email"] == vendor.email
    assert data["store"]["store_name"].startswith("Store")
    assert data["store"]["account_number_masked"] == "******6789"
    assert len(data["products"]) == 1
    assert len(data["orders_as_seller"]) == 1 and len(data["orders_as_buyer"]) == 1
    assert data["orders_as_buyer"][0]["your_details"]["phone"] == "08099998888"
    assert data["orders_as_seller"][0]["items"][0]["quantity"] == 1
    assert len(data["reviews_written"]) == 1 and len(data["reviews_received"]) == 1
    assert data["subscriptions"][0]["plan"] == "PRO"
    assert data["subscription_payments"][0]["reference"] == "ref-1"
    assert data["verification"]["cases"][0]["status"] == "PASSED"
    assert data["passport_settings"]["is_public"] is False
    assert data["notifications"][0]["title"] == "Hello"
    assert set(data["consents"]["current"]) >= {"marketing_email", "analytics"}

    raw = res.data.decode()
    for secret in ("password_hash", vendor.password_hash, "654321", "12345678901", "a" * 64,
                   "prov-ref-secret", "ACCT_internal", "SUB_secret", "tok_secret",
                   "0123456789", "nin.jpg", "reset_otp", "id_number_hash",
                   # the other party's contact details
                   "08011112222", "12 Buyer Street", buyer.email):
        assert secret not in raw, secret


def test_export_is_recorded_and_capped(client, auth_headers, make_user):
    user = make_user()
    for _ in range(3):
        assert client.get("/api/privacy/export", headers=auth_headers(user)).status_code == 200
    res = client.get("/api/privacy/export", headers=auth_headers(user))
    assert res.status_code == 429
    assert res.get_json()["code"] == "EXPORT_LIMIT"
    rows = PrivacyRequest.query.filter_by(user_id=user.id, channel="self_service").all()
    assert len(rows) == 3 and all(r.type == "portability" and r.status == "done" for r in rows)


def test_export_requires_a_user_token(client, superadmin):
    assert client.get("/api/privacy/export").status_code == 401
    assert client.get("/api/privacy/export", headers=_admin_headers(superadmin)).status_code == 403


# ── Consents ────────────────────────────────────────────────────────────────

def test_consents_default_and_append_only(client, auth_headers, make_user):
    user = make_user()
    h = auth_headers(user)
    current = client.get("/api/privacy/consents", headers=h).get_json()["consents"]
    assert current["analytics"]["granted"] is False
    assert current["marketing_email"]["granted"] is True  # existing broadcast flag

    assert client.post("/api/privacy/consents", json={"purpose": "analytics", "granted": True},
                       headers=h).status_code == 201
    assert client.post("/api/privacy/consents", json={"purpose": "analytics", "granted": False},
                       headers=h).status_code == 201
    current = client.get("/api/privacy/consents", headers=h).get_json()["consents"]
    assert current["analytics"]["granted"] is False
    assert current["analytics"]["version"]
    assert Consent.query.filter_by(user_id=user.id, purpose="analytics").count() == 2


@pytest.mark.parametrize("body", [
    {"purpose": "selling_data", "granted": True},
    {"purpose": "analytics", "granted": "yes"},
    {"purpose": "analytics"},
])
def test_consent_validation(client, auth_headers, make_user, body):
    user = make_user()
    assert client.post("/api/privacy/consents", json=body, headers=auth_headers(user)).status_code == 400


def test_marketing_email_consent_syncs_broadcast_flag(client, auth_headers, make_user):
    user = make_user()
    h = auth_headers(user)
    client.post("/api/privacy/consents", json={"purpose": "marketing_email", "granted": False}, headers=h)
    db.session.refresh(user)
    assert user.is_subscribed_to_broadcasts is False
    client.post("/api/privacy/consents", json={"purpose": "marketing_email", "granted": True}, headers=h)
    db.session.refresh(user)
    assert user.is_subscribed_to_broadcasts is True


def test_unsubscribe_link_still_works_and_is_reflected(client, auth_headers, make_user):
    user = make_user()
    h = auth_headers(user)
    client.post("/api/privacy/consents", json={"purpose": "marketing_email", "granted": True}, headers=h)

    secret = os.environ.get("SECRET_KEY", "siiqo-default")
    token = hashlib.sha256(f"{user.email}{secret}".encode()).hexdigest()[:16]
    res = client.get(f"/api/auth/unsubscribe?email={user.email}&token={token}")
    assert res.status_code == 200
    db.session.refresh(user)
    assert user.is_subscribed_to_broadcasts is False

    current = client.get("/api/privacy/consents", headers=h).get_json()["consents"]
    assert current["marketing_email"]["granted"] is False
    assert current["marketing_email"]["source"] == "account_setting"


# ── Privacy requests ────────────────────────────────────────────────────────

def test_create_and_list_own_requests(client, auth_headers, make_user):
    user, other = make_user(), make_user()
    res = client.post("/api/privacy/requests", json={"type": "correction", "details": "My name is wrong"},
                      headers=auth_headers(user))
    assert res.status_code == 201
    body = res.get_json()
    assert body["request"]["status"] == "open" and body["request"]["due_at"]

    res = client.post("/api/privacy/requests", json={"type": "access"}, headers=auth_headers(user))
    assert res.status_code == 201
    assert res.get_json()["self_service"]["path"] == "/api/privacy/export"

    mine = client.get("/api/privacy/requests", headers=auth_headers(user)).get_json()["requests"]
    assert {r["type"] for r in mine} == {"correction", "access"}
    assert client.get("/api/privacy/requests", headers=auth_headers(other)).get_json()["requests"] == []


def test_deletion_request_points_to_self_service(client, auth_headers, make_user):
    user = make_user()
    res = client.post("/api/privacy/requests", json={"type": "deletion"}, headers=auth_headers(user))
    assert res.status_code == 200
    body = res.get_json()
    assert body["created"] is False
    assert body["self_service"]["path"] == "/api/auth/delete-account"
    assert PrivacyRequest.query.count() == 0


@pytest.mark.parametrize("body", [
    {"type": "nonsense"},
    {"type": "objection"},                       # needs details
    {"type": "correction", "details": "x" * 2001},
    {"type": "correction", "details": 123},
])
def test_request_validation(client, auth_headers, make_user, body):
    user = make_user()
    assert client.post("/api/privacy/requests", json=body, headers=auth_headers(user)).status_code == 400


def test_open_request_cap(client, auth_headers, make_user):
    user = make_user()
    h = auth_headers(user)
    for i in range(5):
        assert client.post("/api/privacy/requests", json={"type": "objection", "details": f"r{i}"},
                           headers=h).status_code == 201
    assert client.post("/api/privacy/requests", json={"type": "objection", "details": "more"},
                       headers=h).status_code == 429


def test_admin_list_and_resolve(client, auth_headers, make_user, superadmin):
    user = make_user()
    rid = client.post("/api/privacy/requests", json={"type": "objection", "details": "No profiling"},
                      headers=auth_headers(user)).get_json()["request"]["id"]
    client.get("/api/privacy/export", headers=auth_headers(user))  # self-service rows stay out of the queue

    listed = client.get("/api/privacy/admin/requests?status=open",
                        headers=_admin_headers(superadmin)).get_json()["requests"]
    assert [r["id"] for r in listed] == [rid]
    assert listed[0]["user"]["email"] == user.email

    url = f"/api/privacy/admin/requests/{rid}/resolve"
    assert client.post(url, json={"status": "rejected"}, headers=_admin_headers(superadmin)).status_code == 400
    res = client.post(url, json={"status": "done", "note": "Turned off"}, headers=_admin_headers(superadmin))
    assert res.status_code == 200
    assert res.get_json()["request"]["status"] == "done"
    assert client.post(url, json={"status": "done"}, headers=_admin_headers(superadmin)).status_code == 409

    mine = client.get("/api/privacy/requests", headers=auth_headers(user)).get_json()["requests"]
    resolved = next(r for r in mine if r["id"] == rid)
    assert resolved["resolution_note"] == "Turned off" and resolved["resolved_at"]


def test_admin_endpoints_refuse_non_superadmins(client, auth_headers, make_user):
    user = make_user()
    plain = AdminUser(name="Support", email="support@example.com", role="SUPPORT")
    plain.set_password("not-a-real-password")
    db.session.add(plain)
    db.session.commit()
    for headers in (auth_headers(user), _admin_headers(plain)):
        assert client.get("/api/privacy/admin/requests", headers=headers).status_code == 403
        assert client.post("/api/privacy/admin/requests/1/resolve", json={"status": "done"},
                           headers=headers).status_code == 403


# ── Retention ───────────────────────────────────────────────────────────────

@pytest.fixture
def retention_rows(make_user, make_vendor, make_order):
    from app.models.audit import AdminAuditLog
    from app.models.escrow import DeliveryConfirmationCode
    from app.models.social import Post, PostView
    from app.models.telemetry import PlatformEvent
    from app.models.user import PasswordResetAttempt

    now = _utcnow()
    old_user, new_user = make_user(), make_user()
    db.session.add(PasswordResetAttempt(user_id=old_user.id, otp_fingerprint="f" * 64,
                                        failed_count=2, updated_at=now - timedelta(days=2)))
    db.session.add(PasswordResetAttempt(user_id=new_user.id, otp_fingerprint="e" * 64,
                                        failed_count=1, updated_at=now))

    vendor, _ = make_vendor()
    o1, o2 = make_order(vendor), make_order(vendor)
    db.session.add(DeliveryConfirmationCode(order_id=o1.id, code_hash="h" * 64,
                                            expires_at=now - timedelta(days=8)))
    db.session.add(DeliveryConfirmationCode(order_id=o2.id, code_hash="g" * 64,
                                            expires_at=now - timedelta(days=1)))

    db.session.add(PlatformEvent(event_name="privacy_test_old", timestamp=now - timedelta(days=400),
                                 properties={"q": "phone"}))
    db.session.add(PlatformEvent(event_name="privacy_test_new", timestamp=now - timedelta(days=10),
                                 properties={"q": "shoes"}))

    post = Post(user_id=vendor.id, content="hello")
    db.session.add(post)
    db.session.flush()
    db.session.add(PostView(post_id=post.id, ip_address="203.0.113.57", user_agent="UA",
                            viewed_at=now - timedelta(days=100)))
    db.session.add(PostView(post_id=post.id, ip_address="203.0.113.58", user_agent="UA",
                            viewed_at=now - timedelta(days=1)))

    admin = AdminUser(name="A", email="audit@example.com", role="SUPERADMIN")
    admin.set_password("not-a-real-password")
    db.session.add(admin)
    db.session.flush()
    for days in (400, 5):
        db.session.add(AdminAuditLog(admin_id=admin.id, admin_email=admin.email, admin_role=admin.role,
                                     action="X", resource_type="User", resource_id="1",
                                     ip_address="198.51.100.23", user_agent="UA",
                                     timestamp=(now - timedelta(days=days)).replace(tzinfo=None)))
    db.session.commit()


EXPECTED_COUNTS = {
    "password_reset_attempts_deleted": 1,
    "delivery_confirmation_codes_deleted": 1,
    "platform_events_properties_cleared": 1,
    "post_views_ip_cleared": 1,
    "admin_audit_logs_ip_reduced": 1,
}


def test_retention_dry_run_changes_nothing(retention_rows):
    from app.models.user import PasswordResetAttempt
    report = privacy_retention.run()
    assert report["applied"] is False
    for key, value in EXPECTED_COUNTS.items():
        assert report[key] == value, key
    assert PasswordResetAttempt.query.count() == 2


def test_retention_apply(retention_rows):
    from app.models.audit import AdminAuditLog
    from app.models.escrow import DeliveryConfirmationCode
    from app.models.social import PostView
    from app.models.telemetry import PlatformEvent
    from app.models.user import PasswordResetAttempt

    report = privacy_retention.run(apply=True)
    assert report["applied"] is True
    for key, value in EXPECTED_COUNTS.items():
        assert report[key] == value, key

    assert PasswordResetAttempt.query.count() == 1
    assert DeliveryConfirmationCode.query.count() == 1
    events = (PlatformEvent.query.filter(PlatformEvent.event_name.like("privacy_test_%"))
              .order_by(PlatformEvent.timestamp).all())
    assert len(events) == 2  # counts kept
    assert events[0].properties is None and events[1].properties == {"q": "shoes"}
    views = PostView.query.order_by(PostView.viewed_at).all()
    assert views[0].ip_address is None and views[0].user_agent is None
    assert views[1].ip_address == "203.0.113.58"
    logs = AdminAuditLog.query.order_by(AdminAuditLog.timestamp).all()
    assert logs[0].ip_address == "198.51.100.0" and logs[0].user_agent is None
    assert logs[1].ip_address == "198.51.100.23"

    # A second run finds nothing left to do.
    again = privacy_retention.run(apply=True)
    assert all(again[k] == 0 for k in EXPECTED_COUNTS)


@pytest.mark.parametrize("raw,expected", [
    ("203.0.113.57", "203.0.113.0"),
    ("2001:db8:abcd:12::1", "2001:db8:abcd::"),
    ("not-an-ip", "redacted"),
    (None, None),
])
def test_truncate_ip(raw, expected):
    assert privacy_retention.truncate_ip(raw) == expected


def test_scheduled_retention_is_dry_run_unless_enabled(retention_rows, monkeypatch):
    from app.models.user import PasswordResetAttempt
    monkeypatch.delenv("PRIVACY_RETENTION_APPLY", raising=False)
    assert privacy_retention.run_scheduled()["applied"] is False
    assert PasswordResetAttempt.query.count() == 2
    monkeypatch.setenv("PRIVACY_RETENTION_APPLY", "true")
    assert privacy_retention.run_scheduled()["applied"] is True
