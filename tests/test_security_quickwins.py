"""S2, S4, S8: removed migration endpoints, public partner data, fail-closed webhooks."""
import hashlib
import hmac
import json

import pytest

from app.extensions import db
from app.models.partnerships import PartnerApplication
from app.models.user import UserRole
from app.routes import bridge
from app.services import daya_service, flutterwave_service


# ── S2: unauthenticated migration endpoints are gone ────────────────────────

@pytest.mark.parametrize("path", ["/api/auth/migrate-trigger", "/api/auth/migrate-status"])
def test_migration_endpoints_are_removed(client, path):
    assert client.get(path).status_code == 404


# ── S4: public partner list exposes no private data ─────────────────────────

def test_active_partners_hide_email_bank_and_keys(client, make_user):
    partner = make_user(role=UserRole.PARTNER)
    partner.is_verified = True
    db.session.add(PartnerApplication(
        user_id=partner.id,
        business_name="Partner Ltd",
        status="APPROVED",
        service_type="LOGISTICS",
        state_of_operation="Lagos",
        bank_code="058",
        account_number="0123456789",
        account_name="Partner Ltd",
        pricing_settings={"pricing_model": "FLAT", "flat_rate": 2000,
                          "external_api_key": "secret-partner-key"},
    ))
    db.session.commit()

    res = client.get("/api/logistics/active?role=LOGISTICS")

    assert res.status_code == 200
    body = res.get_data(as_text=True)
    for leaked in ("0123456789", "058", "secret-partner-key", partner.email):
        assert leaked not in body
    partners = res.get_json()["data"]
    listed = [p for p in partners if p["id"] == partner.id]
    assert len(listed) == 1
    assert set(listed[0]) == {"id", "name", "business_name", "service_type", "partner_role",
                              "state", "status", "store_settings"}
    # Fields the seller listing and buyer checkout rely on
    assert listed[0]["business_name"] == "Partner Ltd"
    assert listed[0]["store_settings"]["flat_rate"] == 2000
    assert "external_api_key" not in listed[0]["store_settings"]


# ── S8: webhooks fail closed without a configured secret ────────────────────

def test_flutterwave_signature_rejected_without_secret(monkeypatch):
    monkeypatch.delenv("FLW_WEBHOOK_SECRET", raising=False)
    assert flutterwave_service.verify_webhook_signature(b"{}", "") is False
    assert flutterwave_service.verify_webhook_signature(b"{}", "anything") is False


def test_flutterwave_signature_checked_with_secret(monkeypatch):
    monkeypatch.setenv("FLW_WEBHOOK_SECRET", "flw-test-hash")
    assert flutterwave_service.verify_webhook_signature(b"{}", "flw-test-hash") is True
    assert flutterwave_service.verify_webhook_signature(b"{}", "wrong") is False


def test_daya_signature_rejected_without_secret(monkeypatch):
    monkeypatch.delenv("DAYA_WEBHOOK_SECRET", raising=False)
    assert daya_service.verify_webhook_signature(b"{}", "") is False


def test_daya_signature_checked_with_secret(monkeypatch):
    monkeypatch.setenv("DAYA_WEBHOOK_SECRET", "daya-test-secret")
    good = hmac.new(b"daya-test-secret", b"{}", hashlib.sha256).hexdigest()
    assert daya_service.verify_webhook_signature(b"{}", good) is True
    assert daya_service.verify_webhook_signature(b"{}", "bad") is False


def test_daya_webhook_endpoint_rejects_unsigned_event(client, monkeypatch):
    monkeypatch.delenv("DAYA_WEBHOOK_SECRET", raising=False)
    res = client.post("/api/payments/daya/webhook",
                      data=json.dumps({"event": "deposit.completed"}),
                      content_type="application/json")
    assert res.status_code == 401


def test_flutterwave_webhook_endpoint_rejects_unsigned_event(client, monkeypatch):
    monkeypatch.delenv("FLW_WEBHOOK_SECRET", raising=False)
    res = client.post("/api/payments/flutterwave/webhook",
                      data=json.dumps({"event": "charge.completed"}),
                      content_type="application/json")
    assert res.status_code == 401


def test_paystack_webhook_rejected_without_key(client, monkeypatch):
    monkeypatch.setattr(bridge, "PAYSTACK_SECRET_KEY", "")
    body = json.dumps({"event": "charge.success", "data": {}}).encode()
    forged = hmac.new(b"", body, hashlib.sha512).hexdigest()

    res = client.post("/api/payments/webhook", data=body, content_type="application/json",
                      headers={"x-paystack-signature": forged})

    assert res.status_code == 503


def test_paystack_webhook_signature_checked_with_key(client, monkeypatch):
    monkeypatch.setattr(bridge, "PAYSTACK_SECRET_KEY", "sk_test_dummy")
    body = json.dumps({"event": "unknown.event", "data": {}}).encode()

    bad = client.post("/api/payments/webhook", data=body, content_type="application/json",
                      headers={"x-paystack-signature": "bad"})
    good_sig = hmac.new(b"sk_test_dummy", body, hashlib.sha512).hexdigest()
    good = client.post("/api/payments/webhook", data=body, content_type="application/json",
                       headers={"x-paystack-signature": good_sig})

    assert bad.status_code == 400
    assert good.status_code == 200
