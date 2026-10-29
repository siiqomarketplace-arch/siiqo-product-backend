"""VER-4 / S6: typed NIN or CAC numbers no longer flag a seller for review or
show as verified; public store data has no account email or CAC number;
the old paid document-review application is closed."""
from datetime import datetime, timedelta, timezone

from app.extensions import db
from app.models.user import Storefront


def _breakdown(client, vendor):
    return client.get(f"/api/marketplace/vendor/{vendor.id}/trust-breakdown").get_json()["verified"]


def test_typed_nin_and_cac_are_not_public_verification(client, make_vendor):
    vendor, store = make_vendor()
    vendor.nin = "12345678901"
    store.cac_reg = "RC123456"
    db.session.commit()
    flags = _breakdown(client, vendor)
    assert flags["id_verified"] is False
    assert flags["cac_registered"] is False


def test_legacy_verified_seller_keeps_mark_until_expiry(client, make_vendor):
    vendor, store = make_vendor()
    store.is_pro_verified = True
    store.pro_verified_expires_at = datetime.now(timezone.utc) + timedelta(days=30)
    db.session.commit()
    assert _breakdown(client, vendor)["id_verified"] is True

    store.pro_verified_expires_at = datetime.now(timezone.utc) - timedelta(days=1)
    db.session.commit()
    assert _breakdown(client, vendor)["id_verified"] is False


def test_public_store_hides_email_and_cac_number(client, make_vendor):
    vendor, store = make_vendor()
    store.cac_reg = "RC7654321"
    store.is_published = True
    db.session.commit()
    body = client.get(f"/api/marketplace/store/{store.store_slug}").get_data(as_text=True)
    assert vendor.email not in body
    assert "RC7654321" not in body

    sensitive = db.session.get(Storefront, store.id).to_public_dict(include_sensitive=True)
    assert sensitive["vendor_email"] == vendor.email and sensitive["cac_reg"] == "RC7654321"


def test_settings_update_does_not_flag_pending_review(client, make_vendor, auth_headers):
    vendor, store = make_vendor()
    res = client.patch("/api/vendor/update-settings", data={"cac_reg": "RC1111111"}, headers=auth_headers(vendor))
    assert res.status_code == 200, res.get_json()
    assert db.session.get(Storefront, store.id).verification_status != "PENDING_VERIFY_SUB"


def test_old_paid_application_is_closed(client, make_vendor, auth_headers):
    vendor, _ = make_vendor()
    for path in ("/api/vendor/pro-verified/checkout", "/api/vendor/pro-verified/daya-initiate"):
        res = client.post(path, json={}, headers=auth_headers(vendor))
        assert res.status_code == 410
        assert res.get_json()["coming_soon"] is True
