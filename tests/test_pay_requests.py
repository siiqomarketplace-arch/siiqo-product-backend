"""Safe Pay requests: buyers bring sellers (docs/SAFE_PAY_REQUEST_PLAN.md)."""
from datetime import datetime, timedelta, timezone

import pytest

from app.extensions import db
from app.models.foundations import SellerIdentifier
from app.models.pay_request import PayRequest
from app.models.payment_link import PaymentLink
from app.models.user import Storefront

BANK = {"bank_code": "058", "account_number": "0123456789", "account_name": "ADA OKAFOR", "bank_name": "GTBank"}


@pytest.fixture
def paystack(monkeypatch):
    import app.services.escrow.paystack_provider as provider
    monkeypatch.setattr(provider, "create_paystack_subaccount",
                        lambda business_name, bank_code, account_number, description="":
                        {"success": True, "subaccount_code": "ACCT_test"})


@pytest.fixture
def bank_resolve(monkeypatch):
    def fake(bank_code, account_number, timeout=10):
        from app.services.bank_resolve import BankResolveResult
        return BankResolveResult(status="resolved", account_name="ADAEZE OKAFOR", message="test")
    import app.services.bank_resolve as br
    monkeypatch.setattr(br, "resolve_account", fake)


@pytest.fixture
def emails(monkeypatch):
    sent = []
    import app.utils.email as email_mod
    monkeypatch.setattr(email_mod, "send_siiqo_email", lambda **kw: sent.append(kw))
    return sent


def _create(client, seller="@adafabrics", **extra):
    return client.post("/api/pay-requests", json={"seller": seller, **extra})


def _verified_user(make_user):
    user = make_user()
    user.is_verified = True
    db.session.commit()
    return user


# ── Create ─────────────────────────────────────────────────────────────────
def test_buyer_creates_request_without_account(client):
    res = _create(client, item="2 ankara gowns", amount="15,000", email="buyer@example.com")
    assert res.status_code == 201
    body = res.get_json()
    req = body["request"]
    assert req["status"] == "open" and req["item"] == "2 ankara gowns" and req["amount"] == "15000.00"
    assert req["for"] == "@adafabrics"
    assert "email" not in str(req) and body["buyer_token"]
    row = PayRequest.query.one()
    assert row.identifier_kind == "handle" and row.identifier_hash and row.buyer_email == "buyer@example.com"


def test_phone_is_stored_masked_and_hashed_only(client):
    res = _create(client, seller="0803 123 4567")
    assert res.status_code == 201
    row = PayRequest.query.one()
    assert row.identifier_kind == "phone"
    assert row.identifier_display == "+234 803 *** **67"
    assert "8031234567" not in (row.identifier_display + (row.identifier_hash or ""))
    assert res.get_json()["request"]["for"] is None          # phones never shown on the public page


@pytest.mark.parametrize("payload, code", [
    ({"seller": ""}, "INVALID_SELLER"),
    ({"seller": "@ada", "amount": "-5"}, "INVALID_AMOUNT"),
    ({"seller": "@ada", "amount": "abc"}, "INVALID_AMOUNT"),
    ({"seller": "@ada", "email": "not-an-email"}, "INVALID_EMAIL"),
])
def test_create_validates_input(client, payload, code):
    res = client.post("/api/pay-requests", json=payload)
    assert res.status_code == 400 and res.get_json()["code"] == code


def test_seller_already_on_siiqo_is_not_requested(client, make_vendor):
    vendor, store = make_vendor()
    store.is_published = True
    db.session.add(SellerIdentifier(vendor_id=vendor.id, kind="handle", value="instagram:adafabrics",
                                    source="store"))
    db.session.commit()
    res = _create(client, seller="@adafabrics")
    assert res.status_code == 409
    assert res.get_json() == {"code": "SELLER_ON_SIIQO", "message": "This seller is already on Siiqo.",
                              "store_slug": store.store_slug}


def test_daily_limit_per_buyer(client, monkeypatch):
    from app.services import pay_requests as svc
    monkeypatch.setattr(svc, "MAX_PER_IP_PER_DAY", 2)
    assert _create(client, seller="@a1").status_code == 201
    assert _create(client, seller="@a2").status_code == 201
    res = _create(client, seller="@a3")
    assert res.status_code == 429 and res.get_json()["code"] == "TOO_MANY_REQUESTS"


# ── View and expiry ────────────────────────────────────────────────────────
def test_public_view_and_expiry(client):
    pid = _create(client).get_json()["request"]["id"]
    assert client.get(f"/api/pay-requests/{pid}").get_json()["request"]["status"] == "open"
    row = PayRequest.query.one()
    row.expires_at = datetime.now(timezone.utc) - timedelta(minutes=1)
    db.session.commit()
    assert client.get(f"/api/pay-requests/{pid}").get_json()["request"]["status"] == "expired"
    assert client.get("/api/pay-requests/nope").status_code == 404


# ── Accept ─────────────────────────────────────────────────────────────────
def test_new_seller_accepts_with_name_and_bank(client, make_user, auth_headers, paystack, bank_resolve, emails):
    pid = _create(client, item="2 ankara gowns", amount="15000", email="buyer@example.com").get_json()["request"]["id"]
    seller = _verified_user(make_user)
    h = auth_headers(seller)

    res = client.post(f"/api/pay-requests/{pid}/accept", json={}, headers=h)
    assert res.status_code == 400 and res.get_json()["code"] == "NEEDS_STORE"

    res = client.post(f"/api/pay-requests/{pid}/accept", json={"business_name": "Ada Fabrics", **BANK}, headers=h)
    assert res.status_code == 200, res.get_json()
    req = res.get_json()["request"]
    assert req["status"] == "ready"
    assert req["store"]["url"] == "https://ada-fabrics.siiqo.com"
    assert "/pay/" in req["pay_link_url"]

    store = Storefront.query.filter_by(vendor_id=seller.id).one()
    assert store.store_slug == "ada-fabrics" and store.account_name == "ADAEZE OKAFOR"
    link = PaymentLink.query.filter_by(vendor_id=seller.id).one()
    assert (link.link_type, link.amount, link.buyer_email, link.title) == (
        "INVOICE", 15000, "buyer@example.com", "2 ankara gowns")
    assert len(emails) == 1 and emails[0]["to_email"] == "buyer@example.com"
    assert emails[0]["cta_url"] == req["pay_link_url"]

    # A second tap returns the same result and creates nothing new.
    again = client.post(f"/api/pay-requests/{pid}/accept", json={}, headers=h)
    assert again.status_code == 200 and PaymentLink.query.count() == 1


def test_existing_seller_accepts_in_one_tap(client, make_vendor, auth_headers):
    pid = _create(client, item="Cake").get_json()["request"]["id"]
    vendor, store = make_vendor()
    res = client.post(f"/api/pay-requests/{pid}/accept", json={}, headers=auth_headers(vendor))
    assert res.status_code == 200
    link = PaymentLink.query.one()
    assert link.link_type == "PAY_LINK" and link.amount is None          # no amount: open-amount link
    assert res.get_json()["request"]["store"]["slug"] == store.store_slug


def test_accept_needs_sign_in_and_verified_email(client, make_user, auth_headers):
    pid = _create(client).get_json()["request"]["id"]
    assert client.post(f"/api/pay-requests/{pid}/accept", json={}).status_code == 401
    unverified = make_user()
    res = client.post(f"/api/pay-requests/{pid}/accept", json={"business_name": "X Store", **BANK},
                      headers=auth_headers(unverified))
    assert res.status_code == 403 and res.get_json()["code"] == "EMAIL_NOT_VERIFIED"


def test_taken_store_name_returns_suggestions(client, make_user, make_vendor, auth_headers, bank_resolve):
    _, existing = make_vendor()
    pid = _create(client).get_json()["request"]["id"]
    seller = _verified_user(make_user)
    res = client.post(f"/api/pay-requests/{pid}/accept",
                      json={"business_name": "Ada Store", "store_slug": existing.store_slug, **BANK},
                      headers=auth_headers(seller))
    assert res.status_code == 409 and res.get_json()["code"] == "SLUG_UNAVAILABLE"
    assert Storefront.query.filter_by(vendor_id=seller.id).first() is None


def test_paid_status_comes_from_the_orders(client, make_vendor, auth_headers, make_order):
    pid = _create(client, amount="5000").get_json()["request"]["id"]
    vendor, _ = make_vendor()
    client.post(f"/api/pay-requests/{pid}/accept", json={}, headers=auth_headers(vendor))
    link = PaymentLink.query.one()
    order = make_order(vendor, payment_link_id=link.id)
    assert client.get(f"/api/pay-requests/{pid}").get_json()["request"]["status"] == "ready"
    order.status = "PAID"
    db.session.commit()
    assert client.get(f"/api/pay-requests/{pid}").get_json()["request"]["status"] == "paid"


# ── Decline, report, nudge ─────────────────────────────────────────────────
@pytest.mark.parametrize("action, status", [("decline", "declined"), ("report", "reported")])
def test_seller_can_decline_or_report(client, action, status):
    pid = _create(client).get_json()["request"]["id"]
    res = client.post(f"/api/pay-requests/{pid}/{action}", json={"reason": "not mine"})
    assert res.status_code == 200 and res.get_json()["request"]["status"] == status
    again = client.post(f"/api/pay-requests/{pid}/accept", json={})
    assert again.status_code in (401, 409)


def test_buyer_nudges_once_a_day_with_token(client):
    body = _create(client).get_json()
    pid, token = body["request"]["id"], body["buyer_token"]
    assert client.post(f"/api/pay-requests/{pid}/nudge", json={"buyer_token": "wrong"}).status_code == 403
    assert client.post(f"/api/pay-requests/{pid}/nudge", json={"buyer_token": token}).status_code == 200
    res = client.post(f"/api/pay-requests/{pid}/nudge", json={"buyer_token": token})
    assert res.status_code == 429 and res.get_json()["code"] == "TOO_SOON"


# ── Admin ──────────────────────────────────────────────────────────────────
def test_most_wanted_is_admin_only_and_masks_phones(client, auth_headers, make_user):
    for _ in range(3):
        _create(client, seller="@popular", item="shoes")
    _create(client, seller="08031234567")
    assert client.get("/api/admin/pay-requests/most-wanted", headers=auth_headers(make_user())).status_code == 403

    from flask_jwt_extended import create_access_token
    from app.models.admin import AdminUser
    admin = AdminUser(name="Ops", email="ops@example.com", role="ADMIN")
    admin.set_password("not-a-real-password")
    db.session.add(admin)
    db.session.commit()
    h = {"Authorization": f"Bearer {create_access_token(identity=f'admin:{admin.id}')}"}
    rows = client.get("/api/admin/pay-requests/most-wanted", headers=h).get_json()["businesses"]
    assert rows[0]["display"] == "@popular" and rows[0]["requests"] == 3 and rows[0]["items"] == ["shoes"] * 3
    assert rows[1]["display"] == "+234 803 *** **67"
