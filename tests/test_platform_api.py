"""Phase 2B: Trust Platform API v1 (docs/PLATFORM_API_V1.md).

Keys (test / live / legacy sqp_), fixtures, quotas and tiers, the widget's
origin check, domain and handle ownership (DNS, meta tag, bio code; all
mocked), SSRF refusal, webhooks (signature, replay window, retries, disable),
the Growth subscription (Paystack mocked) and no private data in /v1."""
import hashlib
import hmac
import json
import secrets
import socket
from datetime import datetime, timedelta, timezone

import pytest
from flask_jwt_extended import create_access_token

from app.extensions import db
from app.models.admin import AdminUser, VendorSubscription
from app.models.audit import AdminAuditLog
from app.models.foundations import SellerIdentifier
from app.models.passport import PassportApiClient
from app.models.platform import (OwnershipClaim, PlatformSubscription, WebhookDelivery, WebhookEndpoint)
from app.routes import bridge
from app.services import dns_txt, foundations, ownership
from app.services import passport as passport_svc
from app.services import platform as platform_svc
from app.services import platform_webhooks as webhooks
from app.utils import scraper


# ── helpers ────────────────────────────────────────────────────────────────

def _key(client, user, auth_headers, mode="live"):
    res = client.post("/api/passport/keys", json={"name": f"{mode} key", "mode": mode}, headers=auth_headers(user))
    assert res.status_code == 201, res.get_data(as_text=True)
    return res.get_json()


def _bearer(raw):
    return {"Authorization": f"Bearer {raw}"}


def _verified_domain(vendor_id, domain, method="meta"):
    claim = OwnershipClaim(vendor_id=vendor_id, kind="domain", network="", value=domain,
                           token=secrets.token_hex(16), status="verified", method=method,
                           verified_at=datetime.now(timezone.utc))
    db.session.add(claim)
    db.session.commit()
    return claim


class _Page:
    def __init__(self, text, status=200):
        self.text = text
        self.status_code = status
        self.headers = {}
        self.is_redirect = False


def _public_dns(monkeypatch, ip="93.184.216.34"):
    def fake(host, port, *a, **k):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, port))]
    monkeypatch.setattr(scraper.socket, "getaddrinfo", fake)


# ── keys ───────────────────────────────────────────────────────────────────

def test_test_and_live_keys(client, make_user, auth_headers):
    dev = make_user()
    live = _key(client, dev, auth_headers, "live")
    test = _key(client, dev, auth_headers, "test")
    assert live["api_key"].startswith("sqp_live_") and live["mode"] == "live"
    assert test["api_key"].startswith("sqp_test_") and test["mode"] == "test"
    listed = client.get("/api/platform/keys", headers=auth_headers(dev)).get_json()
    modes = {k["id"]: k["mode"] for k in listed["keys"]}
    assert modes == {live["id"]: "live", test["id"]: "test"}
    assert live["api_key"] not in json.dumps(listed) and test["api_key"] not in json.dumps(listed)
    by_id = {k["id"]: k for k in listed["keys"]}
    assert by_id[test["id"]]["usage"]["tier"] == "test"
    assert by_id[live["id"]]["usage"]["tier"] == "free"
    assert client.post("/api/passport/keys", json={"name": "x", "mode": "prod"},
                       headers=auth_headers(dev)).status_code == 400


def test_legacy_sqp_key_still_works_as_live(client, make_vendor):
    _, store = make_vendor()
    raw = f"sqp_{secrets.token_urlsafe(32)}"     # the format issued before 2B
    legacy = PassportApiClient(name="Old platform", key_prefix=raw[:12], key_hash=passport_svc._key_hash(raw))
    db.session.add(legacy)
    db.session.commit()
    assert passport_svc.key_mode(legacy) == "live"
    res = client.get(f"/v1/businesses/{store.store_slug}/trust", headers=_bearer(raw))
    assert res.status_code == 200 and res.get_json()["business"]["store_slug"] == store.store_slug
    assert client.get(f"/v1/businesses/{store.store_slug}/trust",
                      headers={"X-Siiqo-Api-Key": raw}).status_code == 200
    assert client.get(f"/api/passport/{store.store_slug}", headers={"X-Siiqo-Api-Key": raw}).status_code == 200


def test_missing_and_invalid_keys(client, make_vendor):
    _, store = make_vendor()
    res = client.get(f"/v1/businesses/{store.store_slug}/trust")
    assert res.status_code == 401 and res.get_json()["code"] == "UNAUTHORIZED"
    res = client.get(f"/v1/businesses/{store.store_slug}/trust", headers=_bearer("sqp_live_nope"))
    assert res.status_code == 401 and res.get_json()["code"] == "INVALID_KEY"


def test_test_key_answers_only_from_fixtures(client, make_user, make_vendor, auth_headers):
    dev = make_user()
    _, store = make_vendor()
    raw = _key(client, dev, auth_headers, "test")["api_key"]
    for slug in platform_svc.FIXTURE_SLUGS:
        res = client.get(f"/v1/businesses/{slug}/trust", headers=_bearer(raw))
        assert res.status_code == 200
        body = res.get_json()
        assert body["business"]["store_slug"] == slug and body["business"]["test"] is True
        assert body["statement"] and body["alg"] == "HS256"
        # A test statement never verifies as a real one.
        assert client.post("/api/passport/verify", json={"statement": body["statement"]}).get_json()["valid"] is False
    nf = client.get("/v1/businesses/no-such-fixture/trust", headers=_bearer(raw))
    assert nf.status_code == 404 and nf.get_json()["code"] == "NOT_FOUND"
    # A real seller is invisible to a test key.
    assert client.get(f"/v1/businesses/{store.store_slug}/trust", headers=_bearer(raw)).status_code == 404
    assert client.get(f"/api/passport/{store.store_slug}", headers={"X-Siiqo-Api-Key": raw}).status_code == 403


def test_test_mode_lookup_fixtures(client, make_user, auth_headers):
    raw = _key(client, make_user(), auth_headers, "test")["api_key"]
    cases = {"+2348000000001": "test-trusted-store", "08000000002": "test-new-seller",
             "@test_unverified": "test-unverified", "https://siiqo.com/id/test-trusted-store": "test-trusted-store",
             "058:0000000001": "test-trusted-store"}
    for identifier, slug in cases.items():
        body = client.post("/v1/lookup", json={"identifier": identifier}, headers=_bearer(raw)).get_json()
        assert body["match"] == "found" and body["business"]["store_slug"] == slug, identifier
    body = client.post("/v1/lookup", json={"identifier": "@somebody_else"}, headers=_bearer(raw)).get_json()
    assert body == {"match": "not_on_siiqo"}


def test_superadmin_can_issue_test_keys(client):
    admin = AdminUser(name="Ops", email="ops-plat@example.com", role="SUPERADMIN")
    admin.set_password("not-a-real-password")
    db.session.add(admin)
    db.session.commit()
    headers = {"Authorization": f"Bearer {create_access_token(identity=f'admin:{admin.id}')}"}
    res = client.post("/api/passport/clients", json={"name": "Plugin", "mode": "test"}, headers=headers)
    assert res.status_code == 201 and res.get_json()["api_key"].startswith("sqp_test_")


# ── live endpoints ─────────────────────────────────────────────────────────

def test_live_business_trust(client, make_user, make_vendor, auth_headers):
    vendor, store = make_vendor()
    raw = _key(client, make_user(), auth_headers)["api_key"]
    res = client.get(f"/v1/businesses/{store.store_slug}/trust", headers=_bearer(raw))
    assert res.status_code == 200
    body = res.get_json()
    assert set(body) == {"business", "statement", "alg"}
    assert body["business"]["store_slug"] == store.store_slug
    assert client.post("/api/passport/verify", json={"statement": body["statement"]}).get_json()["valid"] is True
    assert res.headers["X-Siiqo-Quota-Limit"] == "5000" and res.headers["X-Siiqo-Quota-Remaining"] == "4999"
    assert res.headers["X-Siiqo-Quota-Reset"].isdigit()
    assert res.headers["Cache-Control"] == "no-store"

    client.put("/api/passport/me", json={"is_public": False}, headers=auth_headers(vendor))
    hidden = client.get(f"/v1/businesses/{store.store_slug}/trust", headers=_bearer(raw))
    assert hidden.status_code == 404 and hidden.get_json()["code"] == "NOT_FOUND"


def test_live_lookup_matches_like_check(client, make_user, make_vendor, auth_headers):
    vendor, store = make_vendor()
    raw = _key(client, make_user(), auth_headers)["api_key"]
    res = client.post("/v1/lookup", json={"identifier": f"https://siiqo.com/id/{store.store_slug}"},
                      headers=_bearer(raw))
    body = res.get_json()
    assert body["match"] == "found" and body["business"]["store_slug"] == store.store_slug
    assert body["statement"] and "matched_on" not in body and "results" not in body

    db.session.add(SellerIdentifier(vendor_id=vendor.id, kind="payout_account_hash",
                                    value=foundations.payout_account_hash("058", "0123456789"),
                                    source="payout"))
    db.session.commit()
    for payload in ({"identifier": "058:0123456789"}, {"bank_code": "058", "account_number": "0123456789"}):
        body = client.post("/v1/lookup", json=payload, headers=_bearer(raw)).get_json()
        assert body["match"] == "found" and body["business"]["store_slug"] == store.store_slug

    assert client.post("/v1/lookup", json={"identifier": "@nobody_here"},
                       headers=_bearer(raw)).get_json() == {"match": "not_on_siiqo"}


def test_lookup_bad_request_is_not_counted(client, make_user, auth_headers):
    created = _key(client, make_user(), auth_headers)
    raw = created["api_key"]
    res = client.post("/v1/lookup", json={}, headers=_bearer(raw))
    assert res.status_code == 400 and res.get_json()["code"] == "BAD_REQUEST"
    assert platform_svc.usage(created["id"]) == 0


# ── quotas, rates and tiers ────────────────────────────────────────────────

def test_monthly_quota_429(client, make_user, make_vendor, auth_headers, monkeypatch):
    monkeypatch.setitem(platform_svc.TIERS, "free", (2, 1000))
    _, store = make_vendor()
    raw = _key(client, make_user(), auth_headers)["api_key"]
    url = f"/v1/businesses/{store.store_slug}/trust"
    first = client.get(url, headers=_bearer(raw))
    assert first.headers["X-Siiqo-Quota-Remaining"] == "1"
    assert client.get(url, headers=_bearer(raw)).headers["X-Siiqo-Quota-Remaining"] == "0"
    over = client.get(url, headers=_bearer(raw))
    assert over.status_code == 429 and over.get_json()["code"] == "QUOTA_EXCEEDED"
    assert over.headers["X-Siiqo-Quota-Limit"] == "2" and over.headers["X-Siiqo-Quota-Remaining"] == "0"


def test_rate_limit_429(client, make_user, make_vendor, auth_headers, monkeypatch):
    monkeypatch.setitem(platform_svc.TIERS, "free", (1000, 2))
    _, store = make_vendor()
    raw = _key(client, make_user(), auth_headers)["api_key"]
    url = f"/v1/businesses/{store.store_slug}/trust"
    assert client.get(url, headers=_bearer(raw)).status_code == 200
    assert client.get(url, headers=_bearer(raw)).status_code == 200
    limited = client.get(url, headers=_bearer(raw))
    assert limited.status_code == 429 and limited.get_json()["code"] == "RATE_LIMITED"
    assert limited.headers["Retry-After"] == "60" and "X-Siiqo-Quota-Limit" in limited.headers


def test_test_keys_have_their_own_quota(client, make_user, auth_headers, monkeypatch):
    monkeypatch.setattr(platform_svc, "TEST_LIMITS", (1, 100))
    dev = make_user()
    test_raw = _key(client, dev, auth_headers, "test")["api_key"]
    live_raw = _key(client, dev, auth_headers, "live")["api_key"]
    assert client.get("/v1/businesses/test-new-seller/trust", headers=_bearer(test_raw)).status_code == 200
    res = client.get("/v1/businesses/test-new-seller/trust", headers=_bearer(test_raw))
    assert res.status_code == 429 and res.get_json()["code"] == "QUOTA_EXCEEDED"
    assert res.headers["X-Siiqo-Quota-Tier"] == "test"
    assert client.post("/v1/lookup", json={"identifier": "@x_y"}, headers=_bearer(live_raw)).status_code == 200


def _superadmin_headers():
    admin = AdminUser(name="Ops", email=f"ops-{secrets.token_hex(3)}@example.com", role="SUPERADMIN")
    admin.set_password("not-a-real-password")
    db.session.add(admin)
    db.session.commit()
    return admin, {"Authorization": f"Bearer {create_access_token(identity=f'admin:{admin.id}')}"}


def test_scale_tier_set_by_superadmin_and_audited(client, make_user, auth_headers):
    dev = make_user()
    created = _key(client, dev, auth_headers)
    url = f"/api/platform/admin/clients/{created['id']}/tier"
    assert client.put(url, json={"tier": "scale", "monthly_limit": 10, "rate_per_minute": 5},
                      headers=auth_headers(dev)).status_code == 403
    admin, headers = _superadmin_headers()
    res = client.put(url, json={"tier": "scale", "monthly_limit": 2_000_000, "rate_per_minute": 3000,
                                "note": "Agreement 2026-11"}, headers=headers)
    assert res.status_code == 200, res.get_data(as_text=True)
    usage = res.get_json()["key"]["usage"]
    assert usage["tier"] == "scale" and usage["monthly_limit"] == 2_000_000 and usage["rate_per_minute"] == 3000
    log = AdminAuditLog.query.filter_by(action="PLATFORM_TIER_SET").one()
    assert log.admin_id == admin.id and log.resource_id == str(created["id"])
    assert log.details["after"]["monthly_limit"] == 2_000_000

    res = client.put(url, json={"tier": "default"}, headers=headers)
    assert res.get_json()["key"]["usage"]["tier"] == "free"
    assert AdminAuditLog.query.filter_by(action="PLATFORM_TIER_CLEARED").count() == 1
    assert client.put(url, json={"tier": "scale", "monthly_limit": "lots", "rate_per_minute": 1},
                      headers=headers).status_code == 400


# ── widget ─────────────────────────────────────────────────────────────────

def test_widget_origin_check(client, make_vendor, auth_headers):
    vendor, store = make_vendor()
    url = f"/v1/widget/{store.store_slug}"
    assert client.get(f"{url}?origin=https://mystore.com").get_json() == {"allowed": False}
    _verified_domain(vendor.id, "mystore.com", method="meta")

    ok = client.get(f"{url}?origin=https://mystore.com")
    body = ok.get_json()
    assert body["allowed"] is True and body["business"]["store_slug"] == store.store_slug and body["statement"]
    assert ok.headers["Access-Control-Allow-Origin"] == "*"
    assert "Access-Control-Allow-Credentials" not in ok.headers
    assert client.get(f"{url}?origin=https://www.mystore.com").get_json()["allowed"] is True
    assert client.get(f"{url}?origin=https://shop.mystore.com").get_json()["allowed"] is False  # meta: exact host
    assert client.get(f"{url}?origin=https://evilmystore.com").get_json()["allowed"] is False
    assert client.get(f"{url}?origin=https://mystore.com.evil.io").get_json()["allowed"] is False
    assert client.get(url).get_json()["allowed"] is False
    # The browser's Origin header must agree with the query.
    assert client.get(f"{url}?origin=https://mystore.com",
                      headers={"Origin": "https://evil.io"}).get_json()["allowed"] is False
    assert client.get(f"{url}?origin=https://mystore.com",
                      headers={"Origin": "https://mystore.com"}).get_json()["allowed"] is True

    claim = OwnershipClaim.query.filter_by(vendor_id=vendor.id).one()
    claim.method = "dns"
    db.session.commit()
    assert client.get(f"{url}?origin=https://shop.mystore.com").get_json()["allowed"] is True  # DNS: subdomains

    claim.status = "lapsed"
    db.session.commit()
    assert client.get(f"{url}?origin=https://mystore.com").get_json()["allowed"] is False

    claim.status = "verified"
    db.session.commit()
    client.put("/api/passport/me", json={"is_public": False}, headers=auth_headers(vendor))
    assert client.get(f"{url}?origin=https://mystore.com").get_json()["allowed"] is False


def test_widget_fixtures_on_localhost_only(client):
    body = client.get("/v1/widget/test-trusted-store?origin=http://localhost:3000").get_json()
    assert body["allowed"] is True and body["business"]["test"] is True
    assert client.get("/v1/widget/test-trusted-store?origin=https://shop.example").get_json()["allowed"] is False


def test_cors_widget_open_keyed_endpoints_closed(client, make_vendor, make_user, auth_headers):
    _, store = make_vendor()
    pre = client.open(f"/v1/widget/{store.store_slug}", method="OPTIONS",
                      headers={"Origin": "https://mystore.com", "Access-Control-Request-Method": "GET"})
    assert pre.status_code == 204
    assert pre.headers["Access-Control-Allow-Origin"] == "*"
    assert "GET" in pre.headers["Access-Control-Allow-Methods"]
    assert "Access-Control-Allow-Credentials" not in pre.headers

    raw = _key(client, make_user(), auth_headers)["api_key"]
    res = client.get(f"/v1/businesses/{store.store_slug}/trust",
                     headers={**_bearer(raw), "Origin": "https://mystore.com"})
    assert "Access-Control-Allow-Origin" not in res.headers
    res = client.post("/v1/lookup", json={"identifier": "@a_b"},
                      headers={**_bearer(raw), "Origin": "https://mystore.com"})
    assert "Access-Control-Allow-Origin" not in res.headers


# ── ownership ──────────────────────────────────────────────────────────────

def _claim(client, vendor, auth_headers, **body):
    res = client.post("/api/platform/ownership", json=body, headers=auth_headers(vendor))
    assert res.status_code == 201, res.get_data(as_text=True)
    return res.get_json()["claim"]


def _check(client, vendor, auth_headers, claim_id):
    res = client.post(f"/api/platform/ownership/{claim_id}/check", headers=auth_headers(vendor))
    assert res.status_code == 202
    return res.get_json()["claim"]


def _no_fetch(monkeypatch):
    calls = []
    monkeypatch.setattr(scraper.requests, "get", lambda *a, **k: calls.append(a) or _Page(""))
    return calls


def test_dns_txt_method_and_lapse(client, make_vendor, auth_headers, monkeypatch):
    vendor, _ = make_vendor()
    claim = _claim(client, vendor, auth_headers, kind="domain", value="https://www.MyStore.com/about")
    assert claim["value"] == "mystore.com" and claim["status"] == "pending"
    txt = claim["instructions"]["dns"]["value"]
    assert txt.startswith("siiqo-verify=")
    assert claim["instructions"]["meta"]["tag"].startswith('<meta name="siiqo-verify"')

    records = ["v=spf1 -all", txt]
    monkeypatch.setattr(dns_txt, "txt_records", lambda name: list(records))
    _no_fetch(monkeypatch)
    checked = _check(client, vendor, auth_headers, claim["id"])
    assert checked["status"] == "verified" and checked["method"] == "dns"
    ident = SellerIdentifier.query.filter_by(vendor_id=vendor.id, kind="domain", value="mystore.com").one()
    assert ident.source == "verified"

    # The record is removed and the home page has no tag: the claim lapses.
    records.clear()
    monkeypatch.setattr(scraper.requests, "get", lambda *a, **k: _Page("<html></html>"))
    _public_dns(monkeypatch)
    ownership.check_claim(db.session.get(OwnershipClaim, claim["id"]))
    assert db.session.get(OwnershipClaim, claim["id"]).status == "lapsed"
    assert SellerIdentifier.query.filter_by(vendor_id=vendor.id, kind="domain").count() == 0


def test_meta_tag_method(client, make_vendor, auth_headers, monkeypatch):
    vendor, _ = make_vendor()
    claim = _claim(client, vendor, auth_headers, kind="domain", value="shop-example.ng")
    token = db.session.get(OwnershipClaim, claim["id"]).token

    def nx(name):
        raise dns_txt.NXDomain("none")
    monkeypatch.setattr(dns_txt, "txt_records", nx)
    _public_dns(monkeypatch)
    seen = []

    def fake_get(url, **kwargs):
        seen.append((url, kwargs.get("allow_redirects")))
        return _Page(f'<html><head><meta name="siiqo-verify" content="{token}"></head></html>')
    monkeypatch.setattr(scraper.requests, "get", fake_get)
    checked = _check(client, vendor, auth_headers, claim["id"])
    assert checked["status"] == "verified" and checked["method"] == "meta"
    assert seen == [("https://shop-example.ng/", False)]


def test_bio_code_method(client, make_vendor, auth_headers, monkeypatch):
    vendor, _ = make_vendor()
    claim = _claim(client, vendor, auth_headers, kind="handle", network="instagram", value="@My.Store")
    assert claim["value"] == "my.store"
    code = claim["instructions"]["bio"]["code"]
    assert code.startswith("siiqo-") and len(code) == len("siiqo-") + 8
    _public_dns(monkeypatch)
    monkeypatch.setattr(scraper.requests, "get", lambda url, **k: _Page(f"<p>Lagos fabrics. {code.upper()}</p>"))
    checked = _check(client, vendor, auth_headers, claim["id"])
    assert checked["status"] == "verified" and checked["method"] == "bio"
    assert SellerIdentifier.query.filter_by(vendor_id=vendor.id, kind="handle",
                                            value="instagram:my.store").count() == 1

    monkeypatch.setattr(scraper.requests, "get", lambda url, **k: _Page("<p>bio changed</p>"))
    ownership.check_claim(db.session.get(OwnershipClaim, claim["id"]))
    assert db.session.get(OwnershipClaim, claim["id"]).status == "lapsed"


def test_ssrf_private_address_never_fetched(client, make_vendor, auth_headers, monkeypatch):
    vendor, _ = make_vendor()
    claim = _claim(client, vendor, auth_headers, kind="domain", value="internal-trick.com")

    def nx(name):
        raise dns_txt.NXDomain("none")
    monkeypatch.setattr(dns_txt, "txt_records", nx)
    _public_dns(monkeypatch, ip="169.254.169.254")
    calls = _no_fetch(monkeypatch)
    checked = _check(client, vendor, auth_headers, claim["id"])
    assert checked["status"] == "pending" and "unsafe" in checked["last_result"]
    assert calls == []


def test_ownership_inputs_and_owner_only(client, make_vendor, make_user, auth_headers):
    vendor, _ = make_vendor()
    other, _ = make_vendor()
    for body in ({"kind": "domain", "value": "localhost"}, {"kind": "domain", "value": "10.0.0.1"},
                 {"kind": "domain", "value": "siiqo.com"}, {"kind": "handle", "network": "myspace", "value": "a"},
                 {"kind": "handle", "network": "x", "value": "bad handle!"}):
        assert client.post("/api/platform/ownership", json=body, headers=auth_headers(vendor)).status_code == 400
    claim = _claim(client, vendor, auth_headers, kind="domain", value="mine.com")
    assert client.post(f"/api/platform/ownership/{claim['id']}/check", headers=auth_headers(other)).status_code == 404
    assert client.delete(f"/api/platform/ownership/{claim['id']}", headers=auth_headers(other)).status_code == 404
    buyer = make_user()
    assert client.get("/api/platform/ownership", headers=auth_headers(buyer)).status_code == 403
    assert client.delete(f"/api/platform/ownership/{claim['id']}", headers=auth_headers(vendor)).status_code == 200


def test_rechecks_are_queued_as_jobs(make_vendor, monkeypatch):
    from app.models.jobs import Job
    vendor, _ = make_vendor()
    claim = _verified_domain(vendor.id, "recheck.com", method="dns")
    claim.last_checked_at = datetime.now(timezone.utc) - timedelta(days=2)
    db.session.commit()
    monkeypatch.setattr(dns_txt, "txt_records", lambda name: [f"siiqo-verify={claim.token}"])
    assert ownership.run_ownership_rechecks() == 1
    assert Job.query.filter_by(type="ownership_check").count() == 1
    assert db.session.get(OwnershipClaim, claim.id).status == "verified"
    assert ownership.run_ownership_rechecks() == 0  # just checked


def test_dns_wire_format_roundtrip():
    q = dns_txt.build_query("example.com", 0x1234)
    # Answer: header + question + one TXT record using a name pointer.
    answer = (bytes.fromhex("123481800001000100000000") + q[12:] + bytes.fromhex("c00c0010000100000e10") + bytes([0, 26, 25]) + b"siiqo-verify=abcdef012345")
    records, truncated = dns_txt.parse_response(answer, 0x1234)
    assert records == ["siiqo-verify=abcdef012345"] and truncated is False
    with pytest.raises(dns_txt.NXDomain):
        dns_txt.parse_response(bytes.fromhex("123481830001000000000000") + q[12:], 0x1234)


# ── webhooks ───────────────────────────────────────────────────────────────

def test_signature_and_replay_window():
    secret, body = "whsec_test", '{"id":"evt_1"}'
    header = webhooks.signature_header(secret, body, timestamp=1_800_000_000)
    t, v1 = header.split(",")
    assert t == "t=1800000000"
    assert v1 == "v1=" + hmac.new(secret.encode(), b"1800000000." + body.encode(), hashlib.sha256).hexdigest()
    assert webhooks.verify_signature(secret, header, body, now=1_800_000_000 + 299)
    assert not webhooks.verify_signature(secret, header, body, now=1_800_000_000 + 301)   # older than 5 min
    assert not webhooks.verify_signature(secret, header, body + " ", now=1_800_000_000)
    assert not webhooks.verify_signature("whsec_other", header, body, now=1_800_000_000)


def _capture_posts(monkeypatch, status=200):
    sent = []

    class R:
        status_code = status

    def fake(url, body, headers):
        sent.append({"url": url, "body": body, "headers": headers})
        return R()
    monkeypatch.setattr(webhooks, "_post", fake)
    return sent


def test_webhook_crud_and_secret_once(client, make_user, auth_headers):
    raw = _key(client, make_user(), auth_headers)["api_key"]
    for bad in ("http://hooks.example.com/x", "https://127.0.0.1/x", "https://localhost/x", "ftp://x.com"):
        res = client.post("/v1/webhooks", json={"url": bad}, headers=_bearer(raw))
        assert res.status_code == 400 and res.get_json()["code"] == "INVALID_URL"
    res = client.post("/v1/webhooks", json={"url": "https://hooks.example.com/siiqo", "events": ["nope"]},
                      headers=_bearer(raw))
    assert res.status_code == 400
    res = client.post("/v1/webhooks", json={"url": "https://hooks.example.com/siiqo"}, headers=_bearer(raw))
    assert res.status_code == 201
    created = res.get_json()
    assert created["secret"].startswith("whsec_") and sorted(created["webhook"]["events"]) == sorted(webhooks.EVENTS)
    listed = client.get("/v1/webhooks", headers=_bearer(raw))
    assert created["secret"] not in listed.get_data(as_text=True)
    assert WebhookEndpoint.query.one().secret_enc != created["secret"]

    other = _key(client, make_user(), auth_headers)["api_key"]
    wid = created["webhook"]["id"]
    assert client.delete(f"/v1/webhooks/{wid}", headers=_bearer(other)).status_code == 404
    assert client.delete(f"/v1/webhooks/{wid}", headers=_bearer(raw)).status_code == 200
    assert client.get("/v1/webhooks", headers=_bearer(raw)).get_json()["webhooks"] == []


def test_passport_hidden_event_delivered_and_signed(client, make_user, make_vendor, auth_headers, monkeypatch):
    vendor, store = make_vendor()
    raw = _key(client, make_user(), auth_headers)["api_key"]
    created = client.post("/v1/webhooks", json={"url": "https://hooks.example.com/siiqo"},
                          headers=_bearer(raw)).get_json()
    sent = _capture_posts(monkeypatch)
    client.put("/api/passport/me", json={"is_public": False}, headers=auth_headers(vendor))
    assert len(sent) == 1
    body = json.loads(sent[0]["body"])
    assert body["type"] == "business.passport_hidden" and body["data"] == {"slug": store.store_slug}
    assert set(body) == {"id", "type", "created_at", "data"}
    assert webhooks.verify_signature(created["secret"], sent[0]["headers"]["Siiqo-Signature"], sent[0]["body"])
    log = client.get(f"/v1/webhooks/{created['webhook']['id']}/deliveries", headers=_bearer(raw)).get_json()
    assert log["deliveries"][0]["status"] == "delivered" and log["deliveries"][0]["attempts"] == 1


def test_change_detection_events(make_vendor, monkeypatch, client, make_user, auth_headers):
    vendor, store = make_vendor()
    raw = _key(client, make_user(), auth_headers)["api_key"]
    client.post("/v1/webhooks", json={"url": "https://hooks.example.com/siiqo"}, headers=_bearer(raw))
    sent = _capture_posts(monkeypatch)
    state = {"visible": True, "slug": store.store_slug, "tier": "SILVER", "band": 5, "verified": False,
             "claims": {"store_slug": store.store_slug, "tier": "SILVER"}}
    monkeypatch.setattr(webhooks, "_current_state", lambda vid: dict(state))

    def types():
        out = [json.loads(s["body"])["type"] for s in sent]
        sent.clear()
        return out

    assert webhooks.evaluate(vendor.id) == [] and types() == []           # baseline
    state.update(band=6)
    assert types() == [] and webhooks.evaluate(vendor.id) == ["business.trust_changed"]
    assert types() == ["business.trust_changed"]
    state.update(verified=True)
    assert webhooks.evaluate(vendor.id) == ["business.verified"]
    state.update(verified=False)
    assert webhooks.evaluate(vendor.id) == ["business.verification_lapsed"]
    assert webhooks.evaluate(vendor.id) == []                              # nothing changed
    state.update(visible=False, claims=None)
    assert webhooks.evaluate(vendor.id) == ["business.passport_hidden"]
    types()


def test_test_key_endpoints_get_no_real_events_but_can_ping(client, make_user, make_vendor, auth_headers,
                                                            monkeypatch):
    vendor, _ = make_vendor()
    raw = _key(client, make_user(), auth_headers, "test")["api_key"]
    created = client.post("/v1/webhooks", json={"url": "https://hooks.example.com/t"}, headers=_bearer(raw)).get_json()
    sent = _capture_posts(monkeypatch)
    client.put("/api/passport/me", json={"is_public": False}, headers=auth_headers(vendor))
    assert sent == []
    res = client.post(f"/v1/webhooks/{created['webhook']['id']}/test", headers=_bearer(raw))
    assert res.status_code == 202 and len(sent) == 1
    assert json.loads(sent[0]["body"])["data"]["test"] is True


def test_no_webhook_jobs_without_endpoints(make_vendor):
    from app.models.jobs import Job
    vendor, _ = make_vendor()
    assert webhooks.notify_passport_change(vendor.id, hint="trust") is None
    assert Job.query.count() == 0


def test_retries_with_backoff_then_disabled(client, make_user, auth_headers, monkeypatch):
    raw = _key(client, make_user(), auth_headers)["api_key"]
    client.post("/v1/webhooks", json={"url": "https://hooks.example.com/down"}, headers=_bearer(raw))
    sent = _capture_posts(monkeypatch, status=500)
    webhooks.emit("business.verified", {"slug": "s"})
    d = WebhookDelivery.query.one()
    gaps = []
    for _ in range(len(webhooks.BACKOFF)):
        db.session.refresh(d)
        assert d.status == "retrying"
        gaps.append(round((d.next_attempt_at.replace(tzinfo=timezone.utc) -
                           d.last_attempt_at.replace(tzinfo=timezone.utc)).total_seconds()))
        assert webhooks.run_due_deliveries(now=datetime.now(timezone.utc) + timedelta(days=2)) == 1
    assert gaps == [60, 300, 1800, 7200, 43200]
    db.session.refresh(d)
    assert d.status == "failed" and d.attempts == 6 and d.last_status_code == 500
    endpoint = WebhookEndpoint.query.one()
    assert endpoint.is_active is False and endpoint.disabled_reason == "too_many_failures"
    assert len(sent) == 6
    webhooks.emit("business.verified", {"slug": "s"})          # disabled: nothing more is sent
    assert len(sent) == 6


def test_a_success_resets_and_keeps_endpoint(client, make_user, auth_headers, monkeypatch):
    raw = _key(client, make_user(), auth_headers)["api_key"]
    client.post("/v1/webhooks", json={"url": "https://hooks.example.com/flaky"}, headers=_bearer(raw))
    _capture_posts(monkeypatch, status=503)
    webhooks.emit("business.verified", {"slug": "s"})
    _capture_posts(monkeypatch, status=200)
    webhooks.run_due_deliveries(now=datetime.now(timezone.utc) + timedelta(minutes=2))
    d = WebhookDelivery.query.one()
    assert d.status == "delivered" and d.attempts == 2
    assert WebhookEndpoint.query.one().is_active is True


def test_webhook_to_private_address_is_refused(client, make_user, auth_headers, monkeypatch):
    raw = _key(client, make_user(), auth_headers)["api_key"]
    client.post("/v1/webhooks", json={"url": "https://hooks.example.com/x"}, headers=_bearer(raw))
    _public_dns(monkeypatch, ip="10.1.2.3")
    posts = []
    monkeypatch.setattr("requests.post", lambda *a, **k: posts.append(a))
    webhooks.emit("business.verified", {"slug": "s"})
    d = WebhookDelivery.query.one()
    assert d.status == "retrying" and d.last_error == "UnsafeURLError" and posts == []


# ── Growth subscription (Paystack mocked) ──────────────────────────────────

PAYSTACK_KEY = "sk_test_not_a_real_key"
GROWTH_PLAN = "PLN_growth_test"


def _paystack(client, event):
    body = json.dumps(event).encode()
    sig = hmac.new(PAYSTACK_KEY.encode(), body, hashlib.sha512).hexdigest()
    return client.post("/api/payments/webhook", data=body,
                       headers={"Content-Type": "application/json", "x-paystack-signature": sig})


def test_growth_subscription_flow(client, make_user, auth_headers, monkeypatch):
    import requests
    monkeypatch.setenv("PAYSTACK_SECRET_KEY", PAYSTACK_KEY)
    monkeypatch.setenv("PAYSTACK_PLAN_PLATFORM_GROWTH", GROWTH_PLAN)
    monkeypatch.setenv("PLATFORM_GROWTH_PRICE_NGN", "25000")
    monkeypatch.setattr(bridge, "PAYSTACK_SECRET_KEY", PAYSTACK_KEY)
    dev = make_user()
    created = _key(client, dev, auth_headers)
    test_key = _key(client, dev, auth_headers, "test")
    calls = []

    class R:
        ok = True

        def json(self):
            return {"status": True, "data": {"authorization_url": "https://checkout.paystack.com/abc",
                                              "reference": calls[-1]["json"]["reference"]}}

    monkeypatch.setattr(requests, "post", lambda url, **kw: calls.append({"url": url, **kw}) or R())
    res = client.post(f"/api/platform/keys/{created['id']}/growth", headers=auth_headers(dev))
    assert res.status_code == 200 and res.get_json()["authorization_url"].startswith("https://checkout.paystack.com")
    sent = calls[0]["json"]
    assert calls[0]["url"].endswith("/transaction/initialize")
    assert sent["plan"] == GROWTH_PLAN and sent["amount"] == 2_500_000
    assert sent["metadata"] == {"type": "platform_growth", "client_id": created["id"], "user_id": dev.id}
    assert client.post(f"/api/platform/keys/{test_key['id']}/growth", headers=auth_headers(dev)).status_code == 400

    reference = sent["reference"]
    event = {"event": "charge.success", "data": {"reference": reference, "status": "success", "amount": 2_500_000,
                                                  "customer": {"email": dev.email}, "metadata": sent["metadata"],
                                                  "plan": {"plan_code": GROWTH_PLAN}}}
    assert _paystack(client, event).status_code == 200
    assert _paystack(client, event).status_code == 200            # Paystack retry: applied once
    client_row = db.session.get(PassportApiClient, created["id"])
    limits = platform_svc.limits_for(client_row)
    assert limits == {"tier": "growth", "monthly_limit": 100_000, "rate_per_minute": 600}
    sub = PlatformSubscription.query.one()
    first_end = sub.end_date
    assert VendorSubscription.query.count() == 0                  # never Verified Pro

    _paystack(client, {"event": "subscription.create", "data": {
        "subscription_code": "SUB_growth1", "email_token": "tok", "plan": {"plan_code": GROWTH_PLAN},
        "customer": {"email": dev.email}, "metadata": sent["metadata"]}})
    db.session.refresh(sub)
    assert sub.paystack_subscription_code == "SUB_growth1"

    # Renewal by invoice (no metadata): extends Growth, not Verified Pro.
    _paystack(client, {"event": "invoice.payment_success", "data": {
        "status": "success", "amount": 2_500_000, "customer": {"email": dev.email},
        "subscription": {"subscription_code": "SUB_growth1"}, "transaction": {"reference": "renew-1"}}})
    db.session.refresh(sub)
    assert sub.end_date > first_end and VendorSubscription.query.count() == 0

    _paystack(client, {"event": "subscription.disable", "data": {
        "subscription_code": "SUB_growth1", "customer": {"email": dev.email}, "plan": {"plan_code": GROWTH_PLAN}}})
    db.session.refresh(sub)
    assert sub.status == "CANCELLED_PENDING_EXPIRY"
    assert platform_svc.limits_for(client_row)["tier"] == "growth"  # until the paid period ends

    listed = client.get("/api/platform/keys", headers=auth_headers(dev)).get_json()
    assert listed["tiers"]["growth"]["price_ngn"] == 25000 and listed["tiers"]["growth"]["available"] is True


def test_growth_unavailable_without_config(client, make_user, auth_headers, monkeypatch):
    monkeypatch.delenv("PAYSTACK_PLAN_PLATFORM_GROWTH", raising=False)
    dev = make_user()
    created = _key(client, dev, auth_headers)
    assert client.post(f"/api/platform/keys/{created['id']}/growth", headers=auth_headers(dev)).status_code == 503
    other = make_user()
    assert client.post(f"/api/platform/keys/{created['id']}/growth", headers=auth_headers(other)).status_code == 404


def test_expired_growth_falls_back_to_free(make_user):
    owner = make_user()
    c, _ = passport_svc.create_api_client("k", owner_user_id=owner.id)
    db.session.add(PlatformSubscription(client_id=c.id, status="ACTIVE",
                                        end_date=datetime.now(timezone.utc) - timedelta(days=4)))
    db.session.commit()
    assert platform_svc.limits_for(c)["tier"] == "free"


# ── public facts only ──────────────────────────────────────────────────────

_PRIVATE_KEYS = {"email", "phone", "first_name", "last_name", "legal_name", "full_name", "nin", "cac",
                 "cac_number", "rc_number", "bank_code", "account_number", "account_name", "documents",
                 "verified_name", "address", "vendor_id", "user_id", "matched_on"}


def _keys(obj):
    if isinstance(obj, dict):
        for k, v in obj.items():
            yield k
            yield from _keys(v)
    elif isinstance(obj, list):
        for v in obj:
            yield from _keys(v)


def test_no_private_data_in_v1(client, make_user, make_vendor, auth_headers):
    vendor, store = make_vendor()
    vendor.phone = "08031234567"
    store.phone = "08031234567"
    db.session.commit()
    _verified_domain(vendor.id, "private-check.com")
    raw = _key(client, make_user(), auth_headers)["api_key"]
    answers = [
        client.get(f"/v1/businesses/{store.store_slug}/trust", headers=_bearer(raw)),
        client.post("/v1/lookup", json={"identifier": "08031234567"}, headers=_bearer(raw)),
        client.post("/v1/lookup", json={"identifier": store.store_slug}, headers=_bearer(raw)),
        client.get(f"/v1/widget/{store.store_slug}?origin=https://private-check.com"),
    ]
    for res in answers:
        assert res.status_code == 200
        body = res.get_json()
        assert body.get("match", "found") == "found" and body.get("allowed", True) is True
        assert not (_PRIVATE_KEYS & set(_keys(body))), set(_keys(body))
        text = res.get_data(as_text=True)
        assert vendor.email not in text and "08031234567" not in text and "2348031234567" not in text
        assert "Test User" not in text
