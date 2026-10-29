"""Youverify adapter: request shape, result mapping, webhook signature, business
lookup, and the selfie submission flow end to end (HTTP faked)."""
import hashlib
import hmac
import json

import pytest

from app.extensions import db
from app.models.verification import CaseStatus, VerificationStatus
from app.services import billing
from app.services.verification import base, youverify_provider as yv
from app.services.verification import service

KEY = "yv_test_not_a_real_key"
NIN = "12345678901"
SELFIE = "data:image/jpeg;base64," + "A" * 2000


class _Resp:
    def __init__(self, status, payload):
        self.status_code, self._payload = status, payload

    def json(self):
        return self._payload


@pytest.fixture
def yv_env(monkeypatch):
    monkeypatch.setenv("YOUVERIFY_SECRET_KEY", KEY)
    monkeypatch.setenv("YOUVERIFY_SANDBOX", "true")
    monkeypatch.setenv("VERIFICATION_PROVIDER", "youverify")
    monkeypatch.setattr(service, "get_provider", lambda: yv.YouverifyProvider())
    monkeypatch.setattr(service, "is_available", lambda user: True)
    calls = []

    def respond(payload, status=200):
        monkeypatch.setattr(yv.requests, "post", lambda url, json=None, headers=None, timeout=None:
                            calls.append((url, json, headers)) or _Resp(status, payload))
    return calls, respond


def _found(**over):
    data = {"id": "yv-123", "status": "found", "firstName": "Ada", "middleName": "N", "lastName": "Obi",
            "selfieValidation": True, "allValidationPassed": True}
    data.update(over)
    return {"success": True, "statusCode": 200, "data": data}


@pytest.mark.parametrize("data,outcome,reason", [
    (_found()["data"], base.Outcome.PASS, None),
    (_found(selfieValidation=False)["data"], base.Outcome.MISMATCH, "FACE_MISMATCH"),
    ({"id": "x", "status": "not_found"}, base.Outcome.MISMATCH, "NIN_NOT_FOUND"),
    ({"id": "x", "status": "failed"}, base.Outcome.INCONCLUSIVE, "PROVIDER_FAILED"),
])
def test_result_mapping(data, outcome, reason):
    r = yv.identity_result_from_data(data)
    assert r.outcome == outcome and r.reason_code == reason
    if outcome == base.Outcome.PASS:
        assert r.full_name == "Ada N Obi"


def test_pending_has_no_result_yet():
    assert yv.identity_result_from_data({"id": "x", "status": "pending"}) is None


def test_webhook_signature(monkeypatch):
    monkeypatch.setenv("YOUVERIFY_SECRET_KEY", KEY)
    body = json.dumps({"event": "identity.verification.completed", "data": {"id": "yv-1"}}).encode()
    good = hmac.new(KEY.encode(), body, hashlib.sha256).hexdigest()
    p = yv.YouverifyProvider()
    assert p.verify_webhook(body, {"x-yv-signature": good}) is True
    assert p.verify_webhook(body, {"x-yv-signature": "0" * 64}) is False
    monkeypatch.delenv("YOUVERIFY_SECRET_KEY")
    assert p.verify_webhook(body, {"x-yv-signature": good}) is False  # fails closed without a key


def _start(vendor):
    billing.apply_subscription_payment(vendor, "PRO_MONTHLY", reference=f"R-{vendor.id}")  # pay first
    return service.start_identity(vendor, seller_type="INDIVIDUAL", nin=NIN, consent=True)


def test_full_flow_pass(make_vendor, yv_env):
    calls, respond = yv_env
    vendor, _ = make_vendor()
    case = _start(vendor)
    assert "/vendor/trust/verify/capture?case=" in case.hosted_url

    respond(_found())
    case = service.submit_identity_selfie(vendor, case.id, nin=NIN, selfie_image=SELFIE)
    assert case.status == CaseStatus.PASSED and case.provider_ref == "yv-123"

    url, body, headers = calls[-1]
    assert url == "https://api.sandbox.youverify.co/v2/api/identity/ng/nin"
    assert headers == {"token": KEY}
    assert body["id"] == NIN and body["isSubjectConsent"] is True
    assert body["validations"]["selfie"]["image"] == SELFIE
    row = VerificationStatus.query.filter_by(user_id=vendor.id).one()
    assert row.source_provider == "youverify" and row.public_flags()["identity_verified"] is True
    assert NIN not in json.dumps(case.to_seller_dict())


def test_selfie_with_different_nin_is_refused(make_vendor, yv_env):
    _, respond = yv_env
    vendor, _ = make_vendor()
    case = _start(vendor)
    with pytest.raises(service.VerificationError) as err:
        service.submit_identity_selfie(vendor, case.id, nin="10987654321", selfie_image=SELFIE)
    assert err.value.code == "NIN_CHANGED"


def test_provider_down_does_not_decide(make_vendor, yv_env, monkeypatch):
    vendor, _ = make_vendor()
    case = _start(vendor)

    def boom(*a, **k):
        raise yv.requests.ConnectionError("down")
    monkeypatch.setattr(yv.requests, "post", boom)
    with pytest.raises(service.VerificationError) as err:
        service.submit_identity_selfie(vendor, case.id, nin=NIN, selfie_image=SELFIE)
    assert err.value.code == "PROVIDER_UNAVAILABLE"
    db.session.refresh(case)
    assert case.status == CaseStatus.IN_PROGRESS


def test_pending_then_signed_webhook_completes(client, make_vendor, yv_env):
    _, respond = yv_env
    vendor, _ = make_vendor()
    case = _start(vendor)
    respond({"success": True, "data": {"id": "yv-777", "status": "pending"}})
    case = service.submit_identity_selfie(vendor, case.id, nin=NIN, selfie_image=SELFIE)
    assert case.status == CaseStatus.PENDING_PROVIDER
    case.provider_ref = "yv-777"
    db.session.commit()

    body = json.dumps({"event": "identity.verification.completed", "apiVersion": "v2",
                       "data": _found(id="yv-777")["data"]}).encode()
    sig = hmac.new(KEY.encode(), body, hashlib.sha256).hexdigest()
    res = client.post("/api/verification/webhook/youverify", data=body,
                      headers={"Content-Type": "application/json", "x-yv-signature": sig})
    assert res.status_code == 200
    db.session.refresh(case)
    assert case.status == CaseStatus.PASSED


def test_business_lookup(monkeypatch):
    monkeypatch.setenv("YOUVERIFY_SECRET_KEY", KEY)
    payload = {"data": {"status": "found", "name": "Ada Foods Ltd", "companyStatus": "ACTIVE",
                        "keyPersonnel": [{"name": "Ada N Obi"}, {"firstName": "Chidi", "lastName": "Eze"}]}}
    calls = []
    monkeypatch.setattr(yv.requests, "post", lambda url, json=None, headers=None, timeout=None:
                        calls.append((url, json)) or _Resp(200, payload))
    r = yv.YouverifyProvider().lookup_business(reg_number="1234567", business_type="COMPANY")
    assert r.outcome == base.Outcome.PASS and r.is_active and r.business_name == "Ada Foods Ltd"
    assert r.people == ["Ada N Obi", "Chidi Eze"]
    assert calls[0][1] == {"registrationNumber": "RC1234567", "countryCode": "NG", "isConsent": True}
