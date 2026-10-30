"""Check a seller: anything a buyer pastes is matched against the identifier
graph and answered with public Passport claims only."""
import pytest

from app.extensions import db
from app.models.foundations import SellerIdentifier
from app.models.payment_link import PaymentLink
from app.models.telemetry import PlatformEvent
from app.services.foundations import payout_account_hash
from app.services.passport import settings_for

PHONE = "08031234567"
BANK, ACCOUNT = "058", "0123456789"


def _ident(vendor_id, kind, value):
    db.session.add(SellerIdentifier(vendor_id=vendor_id, kind=kind, value=value, source="test"))
    db.session.commit()


@pytest.fixture
def seller(make_vendor):
    vendor, store = make_vendor()
    store.store_slug = "mama-ade-fabrics"
    db.session.commit()
    _ident(vendor.id, "phone", "2348031234567")
    _ident(vendor.id, "handle", "instagram:mamaade_fabrics")
    _ident(vendor.id, "handle", "tiktok:mamaade")
    _ident(vendor.id, "domain", "mamaade.ng")
    _ident(vendor.id, "paylink_slug", "ade-x7k2")
    _ident(vendor.id, "payout_account_hash", payout_account_hash(BANK, ACCOUNT))
    return vendor, store


def _check(client, q):
    res = client.get("/api/check", query_string={"q": q})
    assert res.status_code == 200, res.get_data(as_text=True)
    return res.get_json()


@pytest.mark.parametrize("q,query_type,matched_on", [
    ("0803 123 4567", "phone", "phone"),
    ("+234 803 123 4567", "phone", "phone"),
    ("@MamaAde_Fabrics", "handle", "handle"),
    ("https://www.instagram.com/mamaade_fabrics/", "social_url", "handle"),
    ("tiktok.com/@mamaade", "social_url", "handle"),
    ("https://siiqo.com/mama-ade-fabrics", "store_url", "store_slug"),
    ("siiqo.com/id/mama-ade-fabrics", "store_url", "store_slug"),
    ("mama-ade-fabrics.siiqo.com", "store_url", "store_slug"),
    ("https://siiqo.com/pay/ade-x7k2", "paylink", "paylink_slug"),
    ("https://www.mamaade.ng/shop", "website", "domain"),
    ("mama-ade-fabrics", "store_slug", "store_slug"),
])
def test_each_query_type(client, seller, q, query_type, matched_on):
    _, store = seller
    data = _check(client, q)
    assert data["query_type"] == query_type
    assert data["found"] is True
    assert len(data["results"]) == 1
    assert data["results"][0]["matched_on"] == matched_on
    assert data["results"][0]["passport"]["store_slug"] == store.store_slug


def test_store_slug_and_paylink_fall_back_without_identifiers(client, make_vendor):
    vendor, store = make_vendor()
    SellerIdentifier.query.delete()
    db.session.add(PaymentLink(vendor_id=vendor.id, title="Ankara", slug="pl-fallback1"))
    db.session.commit()
    SellerIdentifier.query.delete()
    db.session.commit()
    assert _check(client, store.store_slug)["found"] is True
    assert _check(client, "siiqo.com/pay/pl-fallback1")["found"] is True


def test_hidden_passport_not_returned(client, seller, auth_headers):
    vendor, _ = seller
    settings_for(vendor.id, create=True).is_public = False
    db.session.commit()
    data = _check(client, PHONE)
    assert data["found"] is False and data["results"] == [] and data["message"]


def test_inactive_seller_not_returned(client, seller):
    vendor, _ = seller
    vendor.is_active = False
    db.session.commit()
    assert _check(client, PHONE)["found"] is False


def test_no_pii_in_body(client, seller):
    vendor, _ = seller
    for q in (PHONE, "@mamaade_fabrics", "mamaade.ng"):
        res = client.get("/api/check", query_string={"q": q})
        body = res.get_data(as_text=True)
        for private in (vendor.email, "2348031234567", PHONE, "mamaade_fabrics", "mamaade.ng",
                        payout_account_hash(BANK, ACCOUNT)):
            assert private not in body
        assert set(res.get_json()["results"][0]) == {"passport", "matched_on"}


def test_account_check(client, seller):
    _, store = seller
    res = client.get("/api/check/account", query_string={"bank_code": BANK, "account_number": ACCOUNT})
    data = res.get_json()
    assert res.status_code == 200
    assert data["query_type"] == "account" and data["found"] is True
    assert data["results"][0]["matched_on"] == "payout_account_hash"
    assert data["results"][0]["passport"]["store_slug"] == store.store_slug
    assert ACCOUNT not in res.get_data(as_text=True)

    other = client.get("/api/check/account", query_string={"bank_code": BANK, "account_number": "9999999999"})
    assert other.get_json()["found"] is False
    assert client.get("/api/check/account", query_string={"bank_code": BANK,
                                                           "account_number": "123"}).status_code == 400


def test_unknown_input(client, seller):
    for q in ("hello there, is this legit?", "someone@example.com", "07000000000", "unknown-shop"):
        data = _check(client, q)
        assert data["found"] is False and data["results"] == [] and data["message"]
    assert _check(client, "hello there?")["query_type"] == "unknown"
    assert client.get("/api/check").status_code == 400


def test_records_event_without_query_text(client, seller):
    _check(client, PHONE)
    _check(client, "nothing-here")
    events = PlatformEvent.query.filter_by(event_name="funnel.check").all()
    assert len(events) == 2
    props = sorted((e.properties for e in events), key=lambda p: p["found"])
    assert props == [{"query_type": "store_slug", "found": False}, {"query_type": "phone", "found": True}]


def test_at_most_five_results(client, make_vendor):
    for _ in range(7):
        vendor, _ = make_vendor()
        _ident(vendor.id, "phone", "2348099999999")
    assert len(_check(client, "08099999999")["results"]) == 5
