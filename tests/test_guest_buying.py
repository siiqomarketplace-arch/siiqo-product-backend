"""
Guests can buy without an account (docs/HCD_AUDIT_2026-11.md): refreshing an
expired bank-transfer quote and claiming a free product used to need sign-in.
"""
from decimal import Decimal

import pytest

from app.extensions import db
from app.models.order import Order
from app.models.withdrawal import DayaPayment
from app.services import daya_service


@pytest.fixture
def fake_rate(monkeypatch):
    monkeypatch.setenv("DAYA_API_KEY", "test-key")
    monkeypatch.setattr(daya_service, "get_rate", lambda asset="USDT", side="BUY": {
        "rate_id": "rate_new", "rate": 1600.0, "expires_at": "2099-01-01T00:00:00Z"})


@pytest.fixture
def emails(monkeypatch):
    sent = []
    import app.utils.email as email
    monkeypatch.setattr(email, "send_siiqo_email", lambda **k: sent.append(k) or True)
    return sent


def _payment(make_vendor, make_order, buyer=None, status="EXPIRED"):
    vendor, _ = make_vendor()
    order = make_order(vendor, buyer, total="5000.00")
    dp = DayaPayment(order_id=order.id, buyer_id=buyer.id if buyer else None, payment_type="ngn_onramp",
                     amount_ngn=Decimal("5000.00"), asset="USDT", status=status)
    db.session.add(dp)
    db.session.commit()
    return order, dp


# ── Refresh an expired bank-transfer quote ──────────────────────────────────
def test_guest_can_refresh_their_own_quote(client, make_vendor, make_order, fake_rate):
    order, dp = _payment(make_vendor, make_order)
    res = client.post("/api/payments/daya/refresh-rate", json={"orderId": order.id})
    assert res.status_code == 200, res.get_json()
    assert res.get_json()["rateId"] == "rate_new"
    db.session.expire_all()
    assert db.session.get(DayaPayment, dp.id).status == "PENDING"


def test_guest_cannot_refresh_a_members_payment(client, make_user, make_vendor, make_order, fake_rate):
    order, _ = _payment(make_vendor, make_order, buyer=make_user())
    res = client.post("/api/payments/daya/refresh-rate", json={"orderId": order.id})
    assert res.status_code == 401 and res.get_json()["code"] == "SIGN_IN_REQUIRED"


def test_member_refreshes_only_their_own(client, make_user, make_vendor, make_order, fake_rate, auth_headers):
    owner, other = make_user(), make_user()
    order, _ = _payment(make_vendor, make_order, buyer=owner)
    assert client.post("/api/payments/daya/refresh-rate", json={"orderId": order.id},
                       headers=auth_headers(other)).status_code == 403
    assert client.post("/api/payments/daya/refresh-rate", json={"orderId": order.id},
                       headers=auth_headers(owner)).status_code == 200


def test_completed_payment_is_not_reopened(client, make_vendor, make_order, fake_rate):
    order, _ = _payment(make_vendor, make_order, status="COMPLETED")
    assert client.post("/api/payments/daya/refresh-rate", json={"orderId": order.id}).status_code == 409


# ── Claim a free product without an account ─────────────────────────────────
@pytest.fixture
def free_product(make_vendor, make_product):
    vendor, store = make_vendor()
    return make_product(store, price=0, product_type="digital", is_free=True, file_url="https://files.example/guide.pdf")


def test_guest_claims_free_product_with_email(client, free_product, emails):
    res = client.post(f"/api/marketplace/products/{free_product.id}/claim-free", json={"email": "Ada@Example.com"})
    assert res.status_code == 201, res.get_json()
    assert res.get_json()["download_url"] == "https://files.example/guide.pdf"
    order = Order.query.one()
    assert order.buyer_id is None and order.buyer_email == "ada@example.com" and order.total_amount == 0
    assert any(e["to_email"] == "ada@example.com" for e in emails)
    # Claiming again with the same email returns the same order.
    again = client.post(f"/api/marketplace/products/{free_product.id}/claim-free", json={"email": "ada@example.com"})
    assert again.status_code == 200 and Order.query.count() == 1


def test_guest_free_claim_needs_an_email(client, free_product, emails):
    res = client.post(f"/api/marketplace/products/{free_product.id}/claim-free", json={})
    assert res.status_code == 400 and res.get_json()["code"] == "EMAIL_REQUIRED"


def test_member_still_claims_with_account(client, free_product, make_user, auth_headers, emails):
    user = make_user()
    res = client.post(f"/api/marketplace/products/{free_product.id}/claim-free", json={}, headers=auth_headers(user))
    assert res.status_code == 201 and Order.query.one().buyer_id == user.id
