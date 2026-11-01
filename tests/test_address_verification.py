"""Address verification (V5): flag, mock outcomes, the Passport claim (only when
true and current, never in the score), privacy export, no address in output,
and the Youverify adapter's documented request shape (HTTP faked)."""
import json
from datetime import datetime, timedelta, timezone

import pytest

from app.extensions import db
from app.models.address_verification import AddressStatus, AddressVerification
from app.models.communication import Notification
from app.services import billing, passport as passport_svc, trust_v1
from app.services.address_verification import base, service as addr
from app.services.address_verification import youverify_provider as yva

NIN = "12345678901"
STREET = "Zxqv Unique Crescent"
LANDMARK = "Opposite Qwzx Filling Station"


def _address(building="12"):
    return {"building_number": building, "street": STREET, "landmark": LANDMARK,
            "city": "Ikeja", "state": "Lagos", "lga": "Ikeja"}


def _body(building="12", **over):
    body = {"nin": NIN, "address": _address(building), "consent": True}
    body.update(over)
    return body


@pytest.fixture
def enabled(monkeypatch):
    monkeypatch.setenv("ADDRESS_VERIFICATION_ENABLED", "true")
    monkeypatch.delenv("ADDRESS_VERIFICATION_PROVIDER", raising=False)


@pytest.fixture
def pro_vendor(make_vendor):
    vendor, store = make_vendor()
    billing.apply_subscription_payment(vendor, "PRO_MONTHLY", reference=f"R-{vendor.id}")
    db.session.commit()
    return vendor, store


def _start(client, auth_headers, vendor, building="12"):
    return client.post("/api/verification/address/start", json=_body(building), headers=auth_headers(vendor))


def _refresh(client, auth_headers, vendor):
    return client.post("/api/verification/address/refresh", headers=auth_headers(vendor))


def _verified_row(vendor, provider="youverify", days_ago=1):
    done = datetime.now(timezone.utc) - timedelta(days=days_ago)
    row = AddressVerification(user_id=vendor.id, provider=provider, provider_ref="yv-addr-1",
                              status=AddressStatus.VERIFIED, requested_at=done, completed_at=done,
                              last_checked_at=done)
    db.session.add(row)
    db.session.commit()
    return row


# ── flag ────────────────────────────────────────────────────────────────────

def test_flag_off_by_default(client, auth_headers, pro_vendor, monkeypatch):
    monkeypatch.delenv("ADDRESS_VERIFICATION_ENABLED", raising=False)
    vendor, store = pro_vendor
    res = client.get("/api/verification/address/status", headers=auth_headers(vendor))
    assert res.status_code == 200
    assert res.json["coming_soon"] is True and res.json["available"] is False
    assert res.json["can_start"] is False

    res = _start(client, auth_headers, vendor)
    assert res.status_code == 404 and res.json["code"] == "COMING_SOON"
    assert _refresh(client, auth_headers, vendor).status_code == 404
    assert AddressVerification.query.count() == 0


def test_flag_off_hides_an_existing_claim(pro_vendor, monkeypatch):
    vendor, store = pro_vendor
    _verified_row(vendor)
    monkeypatch.setenv("ADDRESS_VERIFICATION_ENABLED", "true")
    assert passport_svc.build_passport(store).get("address_verified") is True
    monkeypatch.setenv("ADDRESS_VERIFICATION_ENABLED", "false")
    assert "address_verified" not in passport_svc.build_passport(store)


# ── rules ───────────────────────────────────────────────────────────────────

def test_requires_the_existing_verified_pro_plan(client, auth_headers, make_vendor, enabled):
    vendor, _ = make_vendor()
    res = _start(client, auth_headers, vendor)
    assert res.status_code == 402 and res.json["code"] == "PLAN_REQUIRED"


@pytest.mark.parametrize("body,code", [
    (_body(consent=False), "CONSENT_REQUIRED"),
    (_body(nin="123"), "INVALID_NIN"),
    ({"nin": NIN, "consent": True, "address": {"street": STREET}}, "ADDRESS_INCOMPLETE"),
    ({"nin": NIN, "consent": True}, "ADDRESS_REQUIRED"),
])
def test_input_rules(client, auth_headers, pro_vendor, enabled, body, code):
    vendor, _ = pro_vendor
    res = client.post("/api/verification/address/start", json=body, headers=auth_headers(vendor))
    assert res.status_code == 400 and res.json["code"] == code
    assert AddressVerification.query.count() == 0


# ── mock pass and fail ──────────────────────────────────────────────────────

def test_mock_pass(client, auth_headers, pro_vendor, enabled):
    vendor, store = pro_vendor
    res = _start(client, auth_headers, vendor, building="12")
    assert res.status_code == 201
    assert res.json["status"] == "PENDING" and res.json["can_start"] is False
    # A second start while pending is refused.
    assert _start(client, auth_headers, vendor).status_code == 409

    res = _refresh(client, auth_headers, vendor)
    assert res.json["status"] == "VERIFIED"
    assert res.json["address_verified"] is True
    assert res.json["is_test_result"] is True
    assert res.json["affects_trust_score"] is False
    assert res.json["valid_until"]
    # A mock result is never shown publicly (like the identity check's).
    assert res.json["shown_on_passport"] is False
    assert "address_verified" not in passport_svc.build_passport(store)
    # The seller is told, in their language (English fallback).
    note = Notification.query.filter_by(user_id=vendor.id, title="Address verified").one()
    assert note.title == "Address verified"


@pytest.mark.parametrize("building,status,reason", [
    ("19", "NOT_VERIFIED", "ADDRESS_NOT_CONFIRMED"),
    ("28", "UNABLE_TO_VERIFY", "AGENT_UNABLE"),
])
def test_mock_fail(client, auth_headers, pro_vendor, enabled, building, status, reason):
    vendor, store = pro_vendor
    _start(client, auth_headers, vendor, building=building)
    res = _refresh(client, auth_headers, vendor)
    assert res.json["status"] == status and res.json["reason_code"] == reason
    assert res.json["address_verified"] is False
    assert res.json["can_start"] is True
    assert "address_verified" not in passport_svc.build_passport(store)


def test_mock_pending_stays_pending(client, auth_headers, pro_vendor, enabled):
    vendor, _ = pro_vendor
    _start(client, auth_headers, vendor, building="7")
    assert _refresh(client, auth_headers, vendor).json["status"] == "PENDING"
    assert Notification.query.filter(Notification.user_id == vendor.id, Notification.title.like("Address%")).count() == 0


def test_attempt_limit(client, auth_headers, pro_vendor, enabled, monkeypatch):
    monkeypatch.setenv("ADDRESS_VERIFICATION_MAX_ATTEMPTS", "2")
    vendor, _ = pro_vendor
    for _ in range(2):
        _start(client, auth_headers, vendor, building="9")
        _refresh(client, auth_headers, vendor)
    res = _start(client, auth_headers, vendor, building="9")
    assert res.status_code == 429 and res.json["code"] == "TOO_MANY_ATTEMPTS"


# ── the Passport claim: only when true and current ──────────────────────────

def test_claim_only_when_verified_and_current(pro_vendor, enabled, monkeypatch):
    vendor, store = pro_vendor
    assert "address_verified" not in passport_svc.build_passport(store)  # never checked

    row = _verified_row(vendor, days_ago=10)
    assert passport_svc.build_passport(store)["address_verified"] is True

    # Past the validity period: the claim disappears (not shown as false).
    monkeypatch.setenv("ADDRESS_VERIFICATION_VALIDITY_DAYS", "5")
    assert "address_verified" not in passport_svc.build_passport(store)
    monkeypatch.delenv("ADDRESS_VERIFICATION_VALIDITY_DAYS")

    # A newer failed check replaces it.
    db.session.add(AddressVerification(user_id=vendor.id, provider="youverify", provider_ref="yv-2",
                                       status=AddressStatus.NOT_VERIFIED,
                                       completed_at=datetime.now(timezone.utc)))
    db.session.commit()
    assert "address_verified" not in passport_svc.build_passport(store)
    assert row.status == AddressStatus.VERIFIED


def test_status_reports_expired(client, auth_headers, pro_vendor, enabled, monkeypatch):
    vendor, _ = pro_vendor
    _verified_row(vendor, days_ago=400)
    res = client.get("/api/verification/address/status", headers=auth_headers(vendor))
    assert res.json["status"] == "EXPIRED" and res.json["address_verified"] is False


def test_claim_is_not_in_the_score(pro_vendor, enabled, make_order):
    vendor, store = pro_vendor
    for _ in range(3):
        order = make_order(vendor, total=20000)
        order.status = "COMPLETED"
    db.session.commit()
    before_v1 = trust_v1.compute(vendor.id)
    before = passport_svc.build_passport(store)

    _verified_row(vendor)
    after_v1 = trust_v1.compute(vendor.id)
    after = passport_svc.build_passport(store)

    assert after["address_verified"] is True
    assert after_v1["score"] == before_v1["score"] and after_v1["tier"] == before_v1["tier"]
    assert (after["score"], after["tier"]) == (before["score"], before["tier"])
    assert {k: v for k, v in after.items() if k != "address_verified"} == before


def test_trust_score_code_never_reads_address_checks():
    import inspect
    from app.services import trust
    for module in (trust_v1, trust):
        assert "address_verification" not in inspect.getsource(module)


# ── privacy: export, and no address anywhere ────────────────────────────────

def test_privacy_export_includes_outcome_only(client, auth_headers, pro_vendor, enabled):
    vendor, _ = pro_vendor
    _start(client, auth_headers, vendor)
    _refresh(client, auth_headers, vendor)
    res = client.get("/api/privacy/export", headers=auth_headers(vendor))
    assert res.status_code == 200
    data = json.loads(res.data)
    rows = data["address_verification"]
    assert len(rows) == 1
    assert rows[0]["status"] == "VERIFIED" and rows[0]["completed_at"] and rows[0]["requested_at"]
    assert rows[0]["consent_version"] == addr.CONSENT_VERSION
    text = res.data.decode()
    assert STREET not in text and LANDMARK not in text and NIN not in text


def test_no_address_or_nin_stored_or_returned(client, auth_headers, pro_vendor, enabled, monkeypatch):
    vendor, store = pro_vendor
    res = _start(client, auth_headers, vendor)
    outputs = [res.data, _refresh(client, auth_headers, vendor).data,
               client.get("/api/verification/address/status", headers=auth_headers(vendor)).data]

    # Make it public (as if from a real provider) and read every public surface.
    AddressVerification.query.update({"provider": "youverify"})
    db.session.commit()
    monkeypatch.setattr(addr, "get_provider", lambda: yva.YouverifyAddressProvider())
    claims = passport_svc.build_passport(store)
    assert claims["address_verified"] is True
    outputs.append(json.dumps(claims).encode())
    outputs.append(client.get(f"/api/passport/{store.store_slug}").data)

    for body in outputs:
        text = body.decode()
        for secret in (STREET, LANDMARK, NIN, "Ikeja"):
            assert secret not in text, secret

    row = AddressVerification.query.one()
    stored = " ".join(str(getattr(row, c.name)) for c in row.__table__.columns)
    for secret in (STREET, LANDMARK, NIN, "Ikeja", "Lagos"):
        assert secret not in stored


# ── Youverify adapter (documented fields; HTTP faked, no network) ───────────

class _Resp:
    def __init__(self, status, payload):
        self.status_code, self._payload = status, payload

    def json(self):
        return self._payload


def test_youverify_request_shape(monkeypatch):
    monkeypatch.setenv("YOUVERIFY_SECRET_KEY", "yv_test_not_a_real_key")
    monkeypatch.setenv("YOUVERIFY_SANDBOX", "true")
    calls = []
    replies = iter([
        _Resp(201, {"success": True, "data": {"id": "cand-1", "type": "nin"}}),
        _Resp(201, {"success": True, "data": {"id": "addr-1", "referenceId": "r1", "status": "pending",
                                              "taskStatus": "PENDING", "type": "individual"}}),
    ])
    monkeypatch.setattr(yva.requests, "post", lambda url, json=None, headers=None, timeout=None:
                        calls.append((url, json, headers)) or next(replies))
    check = yva.YouverifyAddressProvider().request_check(
        user_id=5, nin=NIN, address=base.AddressInput(**{k: v for k, v in _address().items()}))
    assert check.provider_ref == "addr-1" and check.outcome == base.AddressOutcome.PENDING

    (u1, b1, h1), (u2, b2, h2) = calls
    assert u1 == "https://api.sandbox.youverify.co/v2/api/addresses/candidates/identity"
    assert b1 == {"type": "nin", "idNumber": NIN, "subjectConsent": True}
    assert u2 == "https://api.sandbox.youverify.co/v2/api/addresses/individual/request"
    assert b2["candidateId"] == "cand-1" and b2["subjectConsent"] is True
    assert b2["address"] == {"buildingNumber": "12", "landmark": LANDMARK, "street": STREET,
                             "state": "Lagos", "city": "Ikeja", "lga": "Ikeja"}
    assert h1 == h2 == {"token": "yv_test_not_a_real_key"}


@pytest.mark.parametrize("data,outcome", [
    ({"id": "a", "status": "completed", "taskStatus": "VERIFIED"}, base.AddressOutcome.VERIFIED),
    ({"id": "a", "status": "completed", "taskStatus": "NOT_VERIFIED"}, base.AddressOutcome.NOT_VERIFIED),
    ({"id": "a", "status": "completed", "taskStatus": "UNABLE_TO_VERIFY"}, base.AddressOutcome.UNABLE_TO_VERIFY),
    ({"id": "a", "status": "pending", "taskStatus": "PENDING"}, base.AddressOutcome.PENDING),
    ({"id": "a", "status": "awaiting_qa", "taskStatus": "SOMETHING_NEW"}, base.AddressOutcome.PENDING),
    ({"id": "a", "status": "canceled"}, base.AddressOutcome.CANCELLED),
])
def test_youverify_result_mapping(data, outcome):
    assert yva.check_from_data(data).outcome == outcome


def test_youverify_bad_input_and_outage(client, auth_headers, pro_vendor, enabled, monkeypatch):
    monkeypatch.setenv("YOUVERIFY_SECRET_KEY", "yv_test_not_a_real_key")
    monkeypatch.setattr(addr, "get_provider", lambda: yva.YouverifyAddressProvider())
    vendor, _ = pro_vendor

    monkeypatch.setattr(yva.requests, "post", lambda *a, **k: _Resp(400, {"success": False}))
    res = _start(client, auth_headers, vendor)
    assert res.status_code == 400 and res.json["code"] == "DETAILS_REJECTED"

    monkeypatch.setattr(yva.requests, "post", lambda *a, **k: _Resp(503, {}))
    res = _start(client, auth_headers, vendor)
    assert res.status_code == 503 and res.json["code"] == "PROVIDER_UNAVAILABLE"
    assert AddressVerification.query.count() == 0

    monkeypatch.delenv("YOUVERIFY_SECRET_KEY")
    assert _start(client, auth_headers, vendor).json["code"] == "PROVIDER_UNAVAILABLE"
