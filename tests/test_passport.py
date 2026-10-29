"""Trust Passport: free public card, no made-up score, no private data,
signed statements that platforms can verify, seller sharing choices,
embeddable badge and platform API keys."""
from datetime import datetime, timedelta, timezone

import pytest
from flask_jwt_extended import create_access_token

from app.extensions import db
from app.models.admin import AdminUser
from app.models.trust import VendorTrustProfile
from app.models.verification import VerificationStatus


@pytest.fixture
def seller(make_vendor):
    vendor, store = make_vendor()
    store.cac_reg = "RC7654321"
    vendor.nin = "12345678901"
    db.session.commit()
    return vendor, store


def _complete_order(vendor, make_order):
    order = make_order(vendor, total=1000)
    order.status = "COMPLETED"
    db.session.add(VendorTrustProfile(vendor_id=vendor.id, total_trust_score=742, trust_tier="GOLD"))
    db.session.commit()


def test_new_seller_has_no_made_up_score(client, seller):
    _, store = seller
    data = client.get(f"/api/passport/{store.store_slug}").get_json()
    assert data["passport"]["tier"] == "NEW" and data["passport"]["score"] is None
    assert data["passport"]["verified"] is False


def test_earned_score_and_no_private_data(client, seller, make_order):
    vendor, store = seller
    _complete_order(vendor, make_order)
    res = client.get(f"/api/passport/{store.store_slug}")
    body = res.get_data(as_text=True)
    data = res.get_json()
    assert data["passport"]["score"] == 742 and data["passport"]["tier"] == "GOLD"
    assert data["passport"]["completed_orders"] == 1
    for private in ("RC7654321", "12345678901", vendor.email):
        assert private not in body


def test_payment_alone_is_not_verified_but_passed_check_is(client, seller):
    vendor, store = seller
    now = datetime.now(timezone.utc)
    db.session.add(VerificationStatus(user_id=vendor.id, identity_verified_at=now,
                                      valid_until=now + timedelta(days=365),
                                      source_provider="youverify", subscription_active=True))
    db.session.commit()
    assert client.get(f"/api/passport/{store.store_slug}").get_json()["passport"]["verified"] is True


def test_statement_verifies_and_detects_tampering(client, seller):
    _, store = seller
    statement = client.get(f"/api/passport/{store.store_slug}").get_json()["statement"]
    ok = client.post("/api/passport/verify", json={"statement": statement}).get_json()
    assert ok["valid"] is True and ok["claims"]["store_slug"] == store.store_slug

    header, payload, sig = statement.split(".")
    tampered = ".".join([header, payload, sig[:-2] + ("AA" if sig[-2:] != "AA" else "BB")])
    assert client.post("/api/passport/verify", json={"statement": tampered}).get_json()["valid"] is False


def test_seller_can_hide_passport_and_fields(client, seller, auth_headers):
    vendor, store = seller
    res = client.put("/api/passport/me", json={"show_completed_orders": False}, headers=auth_headers(vendor))
    assert res.status_code == 200
    assert "completed_orders" not in client.get(f"/api/passport/{store.store_slug}").get_json()["passport"]

    client.put("/api/passport/me", json={"is_public": False}, headers=auth_headers(vendor))
    assert client.get(f"/api/passport/{store.store_slug}").status_code == 404
    assert client.get(f"/api/passport/{store.store_slug}/badge.svg").status_code == 404


def test_badge_is_svg(client, seller):
    _, store = seller
    res = client.get(f"/api/passport/{store.store_slug}/badge.svg")
    assert res.status_code == 200 and res.mimetype == "image/svg+xml"
    assert "Siiqo Passport" in res.get_data(as_text=True)


def test_api_keys(client, seller):
    _, store = seller
    admin = AdminUser(name="Ops", email="ops2@example.com", role="SUPERADMIN")
    admin.set_password("not-a-real-password")
    db.session.add(admin)
    db.session.commit()
    headers = {"Authorization": f"Bearer {create_access_token(identity=f'admin:{admin.id}')}"}
    created = client.post("/api/passport/clients", json={"name": "Shopify plugin"}, headers=headers).get_json()
    key = created["api_key"]
    assert client.get(f"/api/passport/{store.store_slug}", headers={"X-Siiqo-Api-Key": key}).status_code == 200
    assert client.get(f"/api/passport/{store.store_slug}",
                      headers={"X-Siiqo-Api-Key": "sqp_wrong"}).status_code == 401


def test_jwks_empty_without_signing_key(client, monkeypatch):
    monkeypatch.delenv("PASSPORT_SIGNING_KEY", raising=False)
    assert client.get("/api/passport/.well-known/jwks.json").get_json() == {"keys": []}
