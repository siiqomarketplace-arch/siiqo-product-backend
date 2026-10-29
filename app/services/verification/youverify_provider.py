"""
Youverify adapter (https://doc.youverify.co).

Env:
- YOUVERIFY_SECRET_KEY   API secret key (sent as the `token` header; also the
                         webhook signing secret).
- YOUVERIFY_SANDBOX      "true" to use https://api.sandbox.youverify.co
                         (default: production https://api.youverify.co).

Identity: Youverify has no hosted page, so the seller takes a selfie on
Siiqo's capture page and we call POST /v2/api/identity/ng/nin with the NIN and
the selfie for face matching. Results may also arrive by webhook
(`identity.verification.completed`, signed with HMAC-SHA256 of the raw body in
the `x-yv-signature` header).

Business: POST /v2/api/verifications/global/company-advance-check (Nigeria).
Youverify does not document the director/shareholder field names, so they are
read defensively; with none found, the case goes on hold for support instead
of passing.
"""
import hashlib
import hmac
import logging
import os

import requests

from .base import (BusinessResult, CheckResult, IdentityResult, IdentitySession, Outcome,
                   VerificationProvider)

logger = logging.getLogger(__name__)

PRODUCTION_URL = "https://api.youverify.co"
SANDBOX_URL = "https://api.sandbox.youverify.co"
TIMEOUT = 30


class ProviderUnavailable(Exception):
    """Youverify couldn't be reached or returned an error; nothing was decided."""


def _base_url():
    sandbox = os.environ.get("YOUVERIFY_SANDBOX", "").strip().lower() in ("1", "true", "yes")
    return SANDBOX_URL if sandbox else PRODUCTION_URL


def _secret():
    return os.environ.get("YOUVERIFY_SECRET_KEY", "").strip()


def _full_name(data):
    parts = [data.get("firstName"), data.get("middleName"), data.get("lastName")]
    name = " ".join(p.strip() for p in parts if isinstance(p, str) and p.strip())
    return name or None


def identity_result_from_data(data) -> IdentityResult | None:
    """Map a Youverify identity record (API response or webhook `data`)."""
    if not isinstance(data, dict):
        return None
    ref = str(data.get("id") or "")
    status = (data.get("status") or "").lower()
    if status == "pending" or not status:
        return None
    if status == "not_found":
        return IdentityResult(provider_ref=ref, outcome=Outcome.MISMATCH, reason_code="NIN_NOT_FOUND",
                              checks=[CheckResult("nin_lookup", "FAIL", detail_code="NOT_FOUND")])
    if status == "failed":
        return IdentityResult(provider_ref=ref, outcome=Outcome.INCONCLUSIVE, reason_code="PROVIDER_FAILED",
                              checks=[CheckResult("nin_lookup", "INCONCLUSIVE", detail_code="FAILED")])
    if status != "found":
        return IdentityResult(provider_ref=ref, outcome=Outcome.INCONCLUSIVE, reason_code=f"STATUS_{status.upper()}")

    checks = [CheckResult("nin_lookup", "PASS")]
    face_ok = data.get("selfieValidation")
    if face_ok is not True:
        checks.append(CheckResult("face_match", "FAIL"))
        return IdentityResult(provider_ref=ref, outcome=Outcome.MISMATCH, full_name=_full_name(data),
                              reason_code="FACE_MISMATCH", checks=checks)
    checks.append(CheckResult("face_match", "PASS"))
    return IdentityResult(provider_ref=ref, outcome=Outcome.PASS, full_name=_full_name(data), checks=checks)


_PEOPLE_KEYS = ("keyPersonnel", "key_personnel", "directors", "affiliates", "shareholders",
                "proprietors", "officers", "personsWithSignificantControl")


def _people_from(data):
    names = []
    for key in _PEOPLE_KEYS:
        for person in data.get(key) or []:
            if isinstance(person, str):
                names.append(person)
            elif isinstance(person, dict):
                name = person.get("name") or person.get("fullName") or _full_name(person)
                if name:
                    names.append(str(name))
    return names


class YouverifyProvider(VerificationProvider):
    name = "youverify"

    def _post(self, path, body):
        key = _secret()
        if not key:
            raise ProviderUnavailable("YOUVERIFY_SECRET_KEY is not set")
        try:
            res = requests.post(f"{_base_url()}{path}", json=body, headers={"token": key}, timeout=TIMEOUT)
        except requests.RequestException as e:
            raise ProviderUnavailable(str(e)) from e
        if res.status_code >= 500 or res.status_code in (401, 403, 429):
            raise ProviderUnavailable(f"HTTP {res.status_code}")
        try:
            return res.status_code, res.json()
        except ValueError as e:
            raise ProviderUnavailable("invalid JSON") from e

    # Identity ────────────────────────────────────────────────────────────

    def start_identity(self, *, case_id, id_number, seller_name, return_url):
        # No hosted page at Youverify: send the seller to Siiqo's selfie capture page.
        site = os.environ.get("SITE_URL", "https://siiqo.com").rstrip("/")
        return IdentitySession(provider_ref="", hosted_url=f"{site}/vendor/trust/verify/capture?case={case_id}")

    def submit_identity(self, *, case_id, id_number, selfie_image) -> IdentityResult | None:
        status, payload = self._post("/v2/api/identity/ng/nin", {
            "id": id_number,
            "isSubjectConsent": True,
            "validations": {"selfie": {"image": selfie_image}},
            "metadata": {"siiqoCaseId": str(case_id)},
        })
        data = (payload or {}).get("data") or {}
        if status >= 400:
            # Bad input (e.g. malformed NIN or unreadable image): let the seller retry.
            return IdentityResult(provider_ref=str(data.get("id") or ""), outcome=Outcome.RETRY,
                                  reason_code=f"REJECTED_{status}")
        return identity_result_from_data(data)

    def fetch_identity_result(self, provider_ref, seller_name=None):
        key = _secret()
        if not provider_ref or not key:
            return None
        try:
            res = requests.get(f"{_base_url()}/v2/api/identity/{provider_ref}", headers={"token": key},
                               timeout=TIMEOUT)
            if res.status_code != 200:
                return None
            return identity_result_from_data((res.json() or {}).get("data"))
        except (requests.RequestException, ValueError) as e:
            logger.warning(f"[YOUVERIFY] fetch {provider_ref} failed: {e}")
            return None

    def verify_webhook(self, raw_body, headers):
        key = _secret()
        signature = (headers.get("x-yv-signature") or "").strip().lower()
        if not key or not signature:
            return False
        expected = hmac.new(key.encode(), raw_body, hashlib.sha256).hexdigest()
        return hmac.compare_digest(expected, signature)

    def parse_identity_webhook(self, payload):
        if (payload or {}).get("event") != "identity.verification.completed":
            return None
        return identity_result_from_data(payload.get("data"))

    # Business ────────────────────────────────────────────────────────────

    def lookup_business(self, *, reg_number, business_type):
        reg = reg_number if reg_number.upper().startswith(("RC", "BN", "IT")) else f"RC{reg_number}"
        try:
            status, payload = self._post("/v2/api/verifications/global/company-advance-check", {
                "registrationNumber": reg, "countryCode": "NG", "isConsent": True,
            })
        except ProviderUnavailable as e:
            logger.warning(f"[YOUVERIFY] business lookup unavailable: {e}")
            return BusinessResult(outcome=Outcome.INCONCLUSIVE, reason_code="PROVIDER_UNAVAILABLE")
        data = (payload or {}).get("data") or {}
        if status >= 400 or (data.get("status") or "").lower() == "not_found":
            return BusinessResult(outcome=Outcome.MISMATCH, reason_code="BUSINESS_NOT_FOUND")
        company_status = (data.get("companyStatus") or data.get("status_of_company") or "").upper()
        return BusinessResult(
            outcome=Outcome.PASS,
            business_name=data.get("name") or data.get("companyName"),
            is_active=company_status in ("ACTIVE", "") and (data.get("status") or "").lower() == "found",
            people=_people_from(data),
        )
