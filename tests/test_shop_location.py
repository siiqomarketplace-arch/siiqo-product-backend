"""Shop location by NIPOST postcode: the free Validator + disassemble flow
(HTTP faked, never level 3), error mapping, consent, flag, who is asked,
Passport/Check output (area only), event venues, no expiry, privacy.

Also the demo scenario for docs/NIPOST_DEMO.md (test_demo_scenario)."""
from datetime import datetime, timedelta, timezone

import pytest

from app.extensions import db
from app.models.event import Event
from app.models.location import BusinessLocation, LocationAudit, LocationStatus
from app.services import passport as passport_svc
from app.services import postcode as nipost
from app.services import shop_location as loc

SANDBOX = "FC-01-A01-KP-27"
KEY = "nipost_test_" + "x" * 24  # fake; never sent anywhere (network is blocked)


class FakeResp:
    def __init__(self, status=200, body=None, headers=None):
        self.status_code = status
        self._body = body if body is not None else {}
        self.headers = headers or {}

    def json(self):
        return self._body


class FakeNipost:
    """Answers like the documented NIPOST endpoints; records every call."""

    def __init__(self):
        self.calls = []
        self.lookup_queue = []  # statuses to return before the normal answer
        self.valid_codes = {"FC01A01KP27", "FC01A01LR01", "FC01A01MH01", "FC01A01MV01", "FC01A01MW01"}

    def get(self, url, params=None, headers=None, timeout=None):
        assert headers["X-API-Key"] == KEY and timeout == nipost.TIMEOUT
        self.calls.append((url, dict(params or {})))
        if url.endswith("/v1/lookup"):
            assert str(params["level"]) == "1"  # only the free Validator, ever
            if self.lookup_queue:
                status = self.lookup_queue.pop(0)
                return FakeResp(status, {"error": {"code": {402: "insufficient_credits",
                                                            403: "level_not_granted"}.get(status, "x"),
                                                   "message": "no"}})
            code = params["code"]
            return FakeResp(200, {"postcode": code, "valid": code in self.valid_codes})
        if url.endswith("/v1/assembly/disassemble"):
            c = params["code"]
            return FakeResp(200, {"state": c[0:2], "lga": c[2:4], "district": c[4:7],
                                  "area": c[7:9], "unit": c[9:11], "address": "SHOULD NOT BE STORED"})
        if url.endswith("/v1/reference/states"):
            return FakeResp(200, {"states": [{"code": "FC", "name": "Federal Capital Territory"},
                                             {"code": "LA", "name": "Lagos"}]})
        if url.endswith("/v1/reference/lgas"):
            return FakeResp(200, {"lgas": [{"code": "01", "name": "AMAC"}]})
        return FakeResp(404, {"error": {"code": "not_found", "message": "no"}})

    def paths(self):
        return [u.split("api.postcode.gov.ng")[1] for u, _ in self.calls]


@pytest.fixture
def fake(monkeypatch):
    monkeypatch.setenv("POSTCODE_VERIFICATION_ENABLED", "true")
    monkeypatch.setenv("NIPOST_API_KEY", KEY)
    monkeypatch.delenv("NIPOST_API_BASE", raising=False)
    monkeypatch.setattr(nipost, "RETRY_BACKOFF_SECONDS", 0)
    nipost.clear_reference_cache()
    f = FakeNipost()
    monkeypatch.setattr(nipost.requests, "get", f.get)
    return f


@pytest.fixture
def seller(make_vendor, make_product):
    vendor, store = make_vendor()
    make_product(store, product_type="physical")
    return vendor, store


def _post(client, auth_headers, user, code=SANDBOX, **over):
    body = {"postcode": code, "consent": True, "method": "widget"}
    body.update(over)
    return client.post("/api/location", json=body, headers=auth_headers(user))


# ── validate ────────────────────────────────────────────────────────────────

def test_normalise_and_dashed():
    assert nipost.normalise("fc-01-a01-kp-27") == "FC01A01KP27"
    assert nipost.normalise("FC 01 A01 KP 27") == "FC01A01KP27"
    assert nipost.normalise("12345") is None and nipost.normalise(None) is None
    assert nipost.dashed("FC02A09DB09") == "FC-02-A09-DB-09"


def test_save_with_sandbox_code(client, auth_headers, seller, fake):
    vendor, store = seller
    res = _post(client, auth_headers, vendor)
    assert res.status_code == 201, res.json
    loc_ = res.json["location"]
    assert loc_["postcode_display"] == SANDBOX and loc_["status"] == "verified"
    assert loc_["area_label"] == "AMAC, FCT" and "public_level" not in loc_
    assert res.json["consent_given"] is True
    assert fake.paths()[:2] == ["/v1/lookup", "/v1/assembly/disassemble"]

    row = BusinessLocation.query.one()
    assert row.postcode == "FC01A01KP27" and row.validation_level == 1 and row.method == "widget"
    assert row.state_name == "Federal Capital Territory" and row.lga_name == "AMAC"
    assert row.unit_code == "27"
    # No expiry date and no exact/area setting: those were cut.
    cols = set(BusinessLocation.__table__.columns.keys())
    assert not cols & {"revalidate_after", "public_level"}


def test_reference_names_are_cached(client, auth_headers, seller, fake):
    vendor, _ = seller
    _post(client, auth_headers, vendor)
    _post(client, auth_headers, vendor, code="FC-01-A01-LR-01")
    assert fake.paths().count("/v1/reference/states") == 1
    # lookups are never cached
    assert fake.paths().count("/v1/lookup") == 2


def test_invalid_code(client, auth_headers, seller, fake):
    vendor, _ = seller
    res = _post(client, auth_headers, vendor, code="FC-99-Z99-ZZ-99")
    assert res.status_code == 400 and res.json["code"] == "POSTCODE_INVALID"
    res = _post(client, auth_headers, vendor, code="not a postcode")
    assert res.status_code == 400 and res.json["code"] == "POSTCODE_INVALID"
    assert BusinessLocation.query.count() == 0
    assert {a.result for a in LocationAudit.query.all()} == {"POSTCODE_INVALID"}


def test_429_retries_once_then_busy(client, auth_headers, seller, fake):
    vendor, _ = seller
    fake.lookup_queue = [429]
    assert _post(client, auth_headers, vendor).status_code == 201  # retry succeeded
    fake.lookup_queue = [429, 429]
    res = _post(client, auth_headers, vendor)
    assert res.status_code == 503 and res.json["code"] == "POSTCODE_BUSY"
    assert res.json["message"] == "NIPOST is busy. Try again in a minute."


@pytest.mark.parametrize("status", [401, 402, 403])
def test_key_credit_level_errors_are_unavailable_never_level_3(client, auth_headers, seller, fake, status):
    vendor, _ = seller
    fake.lookup_queue = [status]
    res = _post(client, auth_headers, vendor)
    assert res.status_code == 503 and res.json["code"] == "POSTCODE_UNAVAILABLE"
    lookups = [p for u, p in fake.calls if u.endswith("/v1/lookup")]
    assert len(lookups) == 1 and all(str(p["level"]) == "1" for p in lookups)
    assert BusinessLocation.query.count() == 0


def test_network_error_and_missing_key(client, auth_headers, seller, fake, monkeypatch):
    vendor, _ = seller

    def boom(*a, **k):
        raise nipost.requests.ConnectionError("down")
    monkeypatch.setattr(nipost.requests, "get", boom)
    res = _post(client, auth_headers, vendor)
    assert res.status_code == 503 and res.json["code"] == "POSTCODE_UNAVAILABLE"
    monkeypatch.delenv("NIPOST_API_KEY")
    assert _post(client, auth_headers, vendor).json["code"] == "POSTCODE_UNAVAILABLE"


def test_consent_required_once(client, auth_headers, seller, fake):
    vendor, _ = seller
    res = _post(client, auth_headers, vendor, consent=False)
    assert res.status_code == 400 and res.json["code"] == "CONSENT_REQUIRED"
    assert fake.calls == []
    assert _post(client, auth_headers, vendor).status_code == 201
    # Re-confirming or changing does not ask again.
    assert _post(client, auth_headers, vendor, code="FC-01-A01-MH-01", consent=False).status_code == 201


def test_rate_limit_per_user(client, auth_headers, seller, fake):
    vendor, _ = seller
    for _ in range(loc.ATTEMPTS_PER_HOUR):
        _post(client, auth_headers, vendor)
    res = _post(client, auth_headers, vendor)
    assert res.status_code == 429 and res.json["code"] == "TOO_MANY_ATTEMPTS"


def test_no_nipost_payloads_stored(client, auth_headers, seller, fake):
    vendor, _ = seller
    _post(client, auth_headers, vendor)
    for model in (BusinessLocation, LocationAudit):
        for row in model.query.all():
            for col in model.__table__.columns:
                value = getattr(row, col.name)
                assert "SHOULD NOT BE STORED" not in str(value)
                assert KEY not in str(value)
    cols = set(BusinessLocation.__table__.columns.keys())
    assert not cols & {"address", "latitude", "longitude", "nin", "token"}


# ── flag ────────────────────────────────────────────────────────────────────

def test_feature_off_hides_everything(client, auth_headers, seller, fake, monkeypatch):
    vendor, store = seller
    _post(client, auth_headers, vendor)
    monkeypatch.setenv("POSTCODE_VERIFICATION_ENABLED", "false")
    h = auth_headers(vendor)
    for res in (client.get("/api/location/status", headers=h), _post(client, auth_headers, vendor),
                client.delete("/api/location", headers=h)):
        assert res.status_code == 404 and res.json["code"] == "FEATURE_OFF"
    claims = passport_svc.build_passport(store)
    assert "location" not in claims and "seller_kind" not in claims
    report = client.get(f"/api/check/report/{store.store_slug}").json
    assert "location" not in report and "seller_kind" not in report


# ── who is asked ────────────────────────────────────────────────────────────

def test_needed_per_seller_type(client, auth_headers, make_vendor, make_product, fake):
    def status(v):
        return client.get("/api/location/status", headers=auth_headers(v)).json

    physical, ps = make_vendor()
    make_product(ps, product_type="physical")
    assert status(physical)["needed"] is True and loc.seller_kind(ps) == "physical"

    digital, ds = make_vendor()
    make_product(ds, product_type="digital")
    assert status(digital)["needed"] is False and loc.seller_kind(ds) == "digital"

    # A service with no street address on the store: done remotely, never asked.
    remote, rs = make_vendor()
    make_product(rs, product_type="service")
    assert status(remote)["needed"] is False and loc.seller_kind(rs) == "digital"

    # A service whose store has a street address: customers come in, so asked.
    salon, ss = make_vendor()
    make_product(ss, product_type="service")
    ss.address = "12 Aminu Kano Crescent, Wuse 2"
    db.session.commit()
    assert status(salon)["needed"] is True and loc.seller_kind(ss) == "physical"

    # A blank address does not count.
    blank, bs = make_vendor()
    make_product(bs, product_type="service")
    bs.address = "   "
    db.session.commit()
    assert status(blank)["needed"] is False

    # An address alone (digital products only) does not make a digital seller physical.
    online, osf = make_vendor()
    make_product(osf, product_type="digital")
    osf.address = "Plot 5, Garki"
    db.session.commit()
    assert status(online)["needed"] is False and loc.seller_kind(osf) == "digital"

    mixed, ms = make_vendor()
    make_product(ms, product_type="physical")
    make_product(ms, product_type="digital")
    assert status(mixed)["needed"] is True and loc.seller_kind(ms) == "mixed"

    pickup, pks = make_vendor()
    make_product(pks, product_type="digital")
    pks.delivery_settings = {"pickup": {"enabled": True, "area": "Wuse 2"}}
    db.session.commit()
    assert status(pickup)["needed"] is True

    # an inactive physical product does not count
    old, os_ = make_vendor()
    make_product(os_, product_type="physical").is_active = False
    db.session.commit()
    assert status(old)["needed"] is False


# ── public output ───────────────────────────────────────────────────────────

def test_area_only_in_passport_and_check(client, auth_headers, seller, fake):
    vendor, store = seller
    _post(client, auth_headers, vendor)
    claims = passport_svc.build_passport(store)
    assert claims["seller_kind"] == "physical"
    assert claims["location"]["verified"] is True and claims["location"]["area_label"] == "AMAC, FCT"
    assert "postcode" not in claims["location"]
    report = client.get(f"/api/check/report/{store.store_slug}").json
    assert report["location"]["area_label"] == "AMAC, FCT" and "postcode" not in report["location"]
    # Asking for "exact" is ignored: a shop's postcode is never public.
    _post(client, auth_headers, vendor, public_level="exact")
    assert "postcode" not in passport_svc.build_passport(store)["location"]
    # The setting endpoint is gone.
    assert client.patch("/api/location", json={"public_level": "exact"},
                        headers=auth_headers(vendor)).status_code == 405


def test_location_does_not_change_the_score(client, auth_headers, seller, fake):
    vendor, store = seller
    before = passport_svc.build_passport(store)
    _post(client, auth_headers, vendor)
    after = passport_svc.build_passport(store)
    assert (before["score"], before["tier"]) == (after["score"], after["tier"])
    assert "address_verified" not in after  # Youverify claim untouched


def test_remove_clears_postcode_now(client, auth_headers, seller, fake):
    vendor, store = seller
    _post(client, auth_headers, vendor)
    res = client.delete("/api/location", headers=auth_headers(vendor))
    assert res.status_code == 200 and res.json["location"] is None
    row = BusinessLocation.query.one()
    assert row.status == LocationStatus.REMOVED and row.postcode is None and row.lga_name is None
    assert "location" not in passport_svc.build_passport(store)
    assert LocationAudit.query.filter_by(action="remove").count() == 1


# ── no expiry ───────────────────────────────────────────────────────────────

def test_postcode_does_not_expire(client, auth_headers, seller, fake):
    vendor, store = seller
    _post(client, auth_headers, vendor)
    row = BusinessLocation.query.one()
    row.validated_at = datetime.now(timezone.utc) - timedelta(days=400)
    db.session.commit()
    assert passport_svc.build_passport(store)["location"]["verified"] is True
    assert client.get("/api/location/status", headers=auth_headers(vendor)).json["location"]["status"] == "verified"
    # Changing it is still one tap (no consent box again).
    res = _post(client, auth_headers, vendor, code="FC-01-A01-LR-01", consent=False)
    assert res.status_code == 201 and res.json["location"]["postcode_display"] == "FC-01-A01-LR-01"


# ── events ──────────────────────────────────────────────────────────────────

def _event(vendor, store):
    ev = Event(storefront_id=store.id, vendor_id=vendor.id, title="Demo night", slug=f"demo-{vendor.id}",
               description="d", start_date=datetime(2026, 11, 21, 18), end_date=datetime(2026, 11, 21, 22),
               event_type="meetup", is_published=True, is_active=True)
    db.session.add(ev)
    db.session.commit()
    return ev


def test_event_venue_owner_only_and_public(client, auth_headers, make_vendor, fake):
    owner, store = make_vendor()
    other, _ = make_vendor()
    ev = _event(owner, store)
    url = f"/api/events/{ev.id}/venue-postcode"

    assert client.post(url, json={"postcode": SANDBOX}, headers=auth_headers(other)).status_code == 403
    res = client.post(url, json={"postcode": SANDBOX}, headers=auth_headers(owner))
    assert res.status_code == 200 and res.json["venue_postcode_verified"] is True
    public = client.get(f"/api/events/{ev.slug}").json["event"]
    assert public["venue_postcode_verified"] is True
    assert public["venue_area_label"] == "AMAC, FCT" and public["venue_postcode_display"] == SANDBOX

    assert client.post(url, json={"postcode": "bad"}, headers=auth_headers(owner)).status_code == 400
    assert client.delete(url, headers=auth_headers(owner)).json["venue_postcode_verified"] is False
    assert client.get(f"/api/events/{ev.slug}").json["event"]["venue_postcode_verified"] is False


def test_event_venue_flag_off(client, auth_headers, make_vendor, monkeypatch):
    monkeypatch.delenv("POSTCODE_VERIFICATION_ENABLED", raising=False)
    owner, store = make_vendor()
    ev = _event(owner, store)
    res = client.post(f"/api/events/{ev.id}/venue-postcode", json={"postcode": SANDBOX},
                      headers=auth_headers(owner))
    assert res.status_code == 404 and res.json["code"] == "FEATURE_OFF"


# ── privacy ─────────────────────────────────────────────────────────────────

def test_privacy_export_and_delete(client, auth_headers, seller, fake):
    from app.routes.privacy import build_export
    from app.services.accounts import anonymise_user

    vendor, store = seller
    _post(client, auth_headers, vendor)
    export = build_export(vendor)["shop_location"]
    assert export["shop_locations"][0]["postcode"] == SANDBOX
    assert export["history"][0]["action"] == "save"

    anonymise_user(vendor)
    assert BusinessLocation.query.count() == 0
    assert all(a.postcode is None for a in LocationAudit.query.all())


# ── demo scenario (docs/NIPOST_DEMO.md) ─────────────────────────────────────

def test_demo_scenario(client, auth_headers, make_vendor, make_product, fake):
    """Ngozi (physical) adds FC-01-A01-KP-27 -> badge; an unverified physical
    seller -> warning; a digital seller -> no warning; an event venue verified."""
    ngozi, ngozi_store = make_vendor()
    make_product(ngozi_store, product_type="physical")
    unverified, unverified_store = make_vendor()
    make_product(unverified_store, product_type="physical")
    digital, digital_store = make_vendor()
    make_product(digital_store, product_type="digital")

    assert _post(client, auth_headers, ngozi).status_code == 201

    def check(store):
        return client.get(f"/api/check/report/{store.store_slug}").json

    simba_sees = check(ngozi_store)
    assert simba_sees["seller_kind"] == "physical" and simba_sees["location"]["verified"] is True
    assert check(unverified_store)["seller_kind"] == "physical" and check(unverified_store)["location"] is None
    assert check(digital_store)["seller_kind"] == "digital" and check(digital_store)["location"] is None

    ev = _event(ngozi, ngozi_store)
    res = client.post(f"/api/events/{ev.id}/venue-postcode", json={"postcode": "FC-01-A01-MW-01"},
                      headers=auth_headers(ngozi))
    assert res.json["venue_postcode_verified"] is True
