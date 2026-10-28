"""Verification v2 (VER-1): flag, identity flow, outcomes, duplicates, business step,
webhook signature, and the rule that mock results are never public."""
import hashlib
import hmac
import json

import pytest

from app.extensions import db
from app.models.verification import (CaseStatus, VerificationCase, VerificationStatus)
from app.models.withdrawal import VendorBankAccount

NIN_PASS = "12345678900"      # last digit 0 -> pass
NIN_DIFF_NAME = "12345678905" # 5 -> pass with a different legal name
NIN_INCONCL = "12345678906"
NIN_FRAUD = "12345678907"
NIN_RETRY = "12345678908"
NIN_MISMATCH = "12345678909"


@pytest.fixture
def seller(make_vendor, monkeypatch):
    """An allowlisted seller (staff testing before public launch)."""
    vendor, store = make_vendor()
    monkeypatch.setenv("VERIFICATION_V2_ALLOWLIST", str(vendor.id))
    monkeypatch.delenv("VERIFICATION_V2_ENABLED", raising=False)
    monkeypatch.setenv("VERIFICATION_PROVIDER", "mock")
    return vendor


def _start(client, headers, nin=NIN_PASS, seller_type="INDIVIDUAL", consent=True):
    return client.post("/api/verification/identity/start",
                       json={"nin": nin, "seller_type": seller_type, "consent": consent},
                       headers=headers)


def _complete(client, headers, case):
    ref = VerificationCase.query.get(case["id"]).provider_ref
    return client.post("/api/verification/mock/complete", json={"ref": ref}, headers=headers)


def _verify(client, headers, nin=NIN_PASS, seller_type="INDIVIDUAL"):
    started = _start(client, headers, nin=nin, seller_type=seller_type)
    assert started.status_code == 201, started.get_json()
    done = _complete(client, headers, started.get_json()["case"])
    assert done.status_code == 200, done.get_json()
    return done.get_json()["case"]


# ── feature flag ────────────────────────────────────────────────────────────

def test_coming_soon_for_non_allowlisted_seller(client, make_vendor, auth_headers, monkeypatch):
    monkeypatch.delenv("VERIFICATION_V2_ENABLED", raising=False)
    monkeypatch.delenv("VERIFICATION_V2_ALLOWLIST", raising=False)
    vendor, _ = make_vendor()
    h = auth_headers(vendor)

    status = client.get("/api/verification/status", headers=h).get_json()
    started = _start(client, h)

    assert status["coming_soon"] is True and status["available"] is False
    assert status["price_ngn_per_month"] == 1999
    assert started.status_code == 403 and started.get_json()["code"] == "COMING_SOON"


def test_public_flag_opens_it_for_everyone(client, make_vendor, auth_headers, monkeypatch):
    monkeypatch.setenv("VERIFICATION_V2_ENABLED", "true")
    monkeypatch.delenv("VERIFICATION_V2_ALLOWLIST", raising=False)
    vendor, _ = make_vendor()

    assert client.get("/api/verification/status",
                      headers=auth_headers(vendor)).get_json()["available"] is True


# ── input rules ─────────────────────────────────────────────────────────────

@pytest.mark.parametrize("body,code", [
    ({"nin": NIN_PASS, "seller_type": "INDIVIDUAL", "consent": False}, "CONSENT_REQUIRED"),
    ({"nin": "123", "seller_type": "INDIVIDUAL", "consent": True}, "INVALID_NIN"),
    ({"nin": NIN_PASS, "seller_type": "COMPANY", "consent": True}, "INVALID_SELLER_TYPE"),
])
def test_start_rejects_bad_input(client, seller, auth_headers, body, code):
    res = client.post("/api/verification/identity/start", json=body, headers=auth_headers(seller))
    assert res.status_code == 400 and res.get_json()["code"] == code


def test_buyer_without_store_is_refused(client, make_user, auth_headers, monkeypatch):
    buyer = make_user()
    monkeypatch.setenv("VERIFICATION_V2_ALLOWLIST", str(buyer.id))
    res = _start(client, auth_headers(buyer))
    assert res.status_code == 400 and res.get_json()["code"] == "NO_STORE"


# ── identity outcomes ───────────────────────────────────────────────────────

def test_identity_pass(client, seller, auth_headers):
    h = auth_headers(seller)
    started = _start(client, h).get_json()["case"]
    assert started["status"] == CaseStatus.IN_PROGRESS
    assert "/vendor/trust/verify/mock?ref=" in started["hosted_url"]

    case = _complete(client, h, started).get_json()["case"]
    status = client.get("/api/verification/status", headers=h).get_json()

    assert case["status"] == CaseStatus.PASSED
    assert status["identity_verified"] is True
    assert status["is_test_result"] is True
    assert status["valid_until"]


@pytest.mark.parametrize("nin,expected,reason", [
    (NIN_RETRY, CaseStatus.FAILED_RETRYABLE, "SELFIE_UNCLEAR"),
    (NIN_MISMATCH, CaseStatus.FAILED, "ID_NOT_FOUND"),
    (NIN_FRAUD, CaseStatus.FAILED_FRAUD, "SPOOF_DETECTED"),
    (NIN_INCONCL, CaseStatus.ON_HOLD, "PROVIDER_INCONCLUSIVE"),
])
def test_identity_failure_outcomes(client, seller, auth_headers, nin, expected, reason):
    case = _verify(client, auth_headers(seller), nin=nin)
    assert case["status"] == expected and case["reason_code"] == reason
    assert VerificationStatus.query.filter_by(user_id=seller.id).first() is None


def test_payout_name_mismatch_is_held(client, seller, auth_headers):
    db.session.add(VendorBankAccount(vendor_id=seller.id, bank_name="Test Bank", bank_code="058",
                                     account_number="0123456789", account_name="Chidi Okafor",
                                     is_default=True))
    db.session.commit()

    case = _verify(client, auth_headers(seller))  # verified name = "Test User"

    assert case["status"] == CaseStatus.ON_HOLD
    assert case["reason_code"] == "PAYOUT_NAME_MISMATCH"


def test_matching_payout_name_passes(client, seller, auth_headers):
    db.session.add(VendorBankAccount(vendor_id=seller.id, bank_name="Test Bank", bank_code="058",
                                     account_number="0123456789", account_name="USER TEST",
                                     is_default=True))
    db.session.commit()

    assert _verify(client, auth_headers(seller))["status"] == CaseStatus.PASSED


def test_retry_allowed_then_attempt_limit(client, seller, auth_headers):
    h = auth_headers(seller)
    for _ in range(3):
        assert _verify(client, h, nin=NIN_RETRY)["status"] == CaseStatus.FAILED_RETRYABLE

    res = _start(client, h)
    assert res.status_code == 429 and res.get_json()["code"] == "TOO_MANY_ATTEMPTS"


def test_unfinished_session_is_resumed_not_duplicated(client, seller, auth_headers):
    h = auth_headers(seller)
    first = _start(client, h).get_json()["case"]
    second = _start(client, h).get_json()["case"]
    assert first["id"] == second["id"]
    assert VerificationCase.query.count() == 1


def test_already_verified_cannot_start_again(client, seller, auth_headers):
    h = auth_headers(seller)
    _verify(client, h)
    res = _start(client, h)
    assert res.status_code == 409


def test_duplicate_identity_on_second_account_is_held(client, seller, make_vendor, auth_headers, monkeypatch):
    _verify(client, auth_headers(seller))
    other, _ = make_vendor()
    monkeypatch.setenv("VERIFICATION_V2_ALLOWLIST", f"{seller.id},{other.id}")

    case = _start(client, auth_headers(other)).get_json()["case"]

    assert case["status"] == CaseStatus.ON_HOLD
    assert case["reason_code"] == "DUPLICATE_IDENTITY"
    assert case["hosted_url"] is None


def test_nin_is_never_stored(client, seller, auth_headers):
    _verify(client, auth_headers(seller))
    case = VerificationCase.query.first()
    stored = json.dumps({c.name: str(getattr(case, c.name)) for c in case.__table__.columns})
    assert NIN_PASS not in stored
    assert case.id_number_hash and len(case.id_number_hash) == 64


def test_final_result_is_not_overwritten(client, seller, auth_headers):
    from app.services.verification import service as ver
    from app.services.verification.base import IdentityResult, Outcome

    _verify(client, auth_headers(seller))
    case = VerificationCase.query.first()
    ver.apply_identity_result(case, IdentityResult(provider_ref=case.provider_ref,
                                                   outcome=Outcome.FRAUD))
    assert VerificationCase.query.first().status == CaseStatus.PASSED


# ── business step ───────────────────────────────────────────────────────────

def _business(client, headers, reg="RC1234560", business_type="COMPANY"):
    return client.post("/api/verification/business/verify",
                       json={"reg_number": reg, "business_type": business_type}, headers=headers)


def test_business_requires_identity_first(client, seller, auth_headers):
    res = _business(client, auth_headers(seller))
    assert res.status_code == 400 and res.get_json()["code"] == "IDENTITY_FIRST"


@pytest.mark.parametrize("reg,expected,reason", [
    ("RC1234560", CaseStatus.PASSED, None),
    ("RC1234567", CaseStatus.ON_HOLD, "NOT_A_DIRECTOR"),
    ("RC1234568", CaseStatus.FAILED, "BUSINESS_INACTIVE"),
    ("RC1234569", CaseStatus.FAILED, "BUSINESS_NOT_FOUND"),
])
def test_business_outcomes(client, seller, auth_headers, reg, expected, reason):
    h = auth_headers(seller)
    _verify(client, h, seller_type="BUSINESS")

    case = _business(client, h, reg=reg).get_json()["case"]
    status = client.get("/api/verification/status", headers=h).get_json()

    assert case["status"] == expected and case["reason_code"] == reason
    assert status["business_verified"] is (expected == CaseStatus.PASSED)


# ── webhook ─────────────────────────────────────────────────────────────────

def test_webhook_rejected_without_secret(client, seller, monkeypatch):
    monkeypatch.delenv("VERIFICATION_WEBHOOK_SECRET", raising=False)
    res = client.post("/api/verification/webhook/mock", data=b"{}", content_type="application/json")
    assert res.status_code == 401


def test_signed_webhook_applies_result(client, seller, auth_headers, monkeypatch):
    monkeypatch.setenv("VERIFICATION_WEBHOOK_SECRET", "whsec_test")
    case = _start(client, auth_headers(seller)).get_json()["case"]
    ref = VerificationCase.query.get(case["id"]).provider_ref
    body = json.dumps({"provider_ref": ref, "seller_name": "Test User"}).encode()
    sig = hmac.new(b"whsec_test", body, hashlib.sha256).hexdigest()

    res = client.post("/api/verification/webhook/mock", data=body, content_type="application/json",
                      headers={"X-Siiqo-Mock-Signature": sig})

    assert res.status_code == 200
    assert VerificationCase.query.get(case["id"]).status == CaseStatus.PASSED


def test_mock_complete_refuses_other_sellers_case(client, seller, make_vendor, auth_headers, monkeypatch):
    case = _start(client, auth_headers(seller)).get_json()["case"]
    other, _ = make_vendor()
    monkeypatch.setenv("VERIFICATION_V2_ALLOWLIST", f"{seller.id},{other.id}")
    ref = VerificationCase.query.get(case["id"]).provider_ref

    res = client.post("/api/verification/mock/complete", json={"ref": ref},
                      headers=auth_headers(other))
    assert res.status_code == 404


# ── legacy badge (V7) ───────────────────────────────────────────────────────

def test_legacy_badge_shown_until_expiry(client, seller, auth_headers):
    from datetime import datetime, timedelta, timezone
    store = seller.storefront
    store.is_pro_verified = True
    store.pro_verified_expires_at = datetime.now(timezone.utc) + timedelta(days=30)
    db.session.commit()

    legacy = client.get("/api/verification/status", headers=auth_headers(seller)).get_json()["legacy"]
    assert legacy["verified"] is True and legacy["expires_at"]

    store.pro_verified_expires_at = datetime.now(timezone.utc) - timedelta(days=1)
    db.session.commit()
    legacy = client.get("/api/verification/status", headers=auth_headers(seller)).get_json()["legacy"]
    assert legacy == {"verified": False, "expires_at": None}


# ── public visibility ───────────────────────────────────────────────────────

def test_mock_results_are_never_public(client, seller, auth_headers):
    _verify(client, auth_headers(seller), seller_type="BUSINESS")
    _business(client, auth_headers(seller))
    flags = VerificationStatus.query.filter_by(user_id=seller.id).first().public_flags()
    assert flags == {"identity_verified": False, "business_registered": False}


def test_real_provider_result_is_public_when_subscribed(seller):
    from datetime import datetime, timedelta, timezone
    now = datetime.now(timezone.utc)
    row = VerificationStatus(user_id=seller.id, identity_verified_at=now,
                             business_verified_at=now, valid_until=now + timedelta(days=365),
                             subscription_active=True, source_provider="smileid")
    assert row.public_flags() == {"identity_verified": True, "business_registered": True}
    row.subscription_active = False
    assert row.public_flags()["identity_verified"] is False
    row.subscription_active, row.valid_until = True, now - timedelta(days=1)
    assert row.public_flags()["identity_verified"] is False
