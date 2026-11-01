"""
Youverify address verification adapter (individual physical address, Nigeria).

STATUS: built from Youverify's published docs only; NOT tested against the
sandbox or production API (no live calls during the build). Check it in the
Youverify sandbox (YOUVERIFY_SANDBOX=true) before switching it on.

Sources read on 31 Oct 2026:
- OpenAPI spec: https://doc.youverify.co/api-reference/specs/quickcheck.json
  (tags "KYC - Address Verification (Individuals)")
- Webhook events: https://doc.youverify.co/webhooks/kyc-webhooks.md

Flow (fields exactly as documented in the spec):
1. POST /v2/api/addresses/candidates/identity
     {type: "nin", idNumber, subjectConsent}            -> data.id (candidate id)
2. POST /v2/api/addresses/individual/request
     {candidateId, subjectConsent, description?, address{flatNumber?,
      buildingName?, buildingNumber, landmark, street, subStreet?, state,
      city, lga?}, metadata?}                            -> data.id, data.status, data.taskStatus
3. GET  /v2/api/addresses/{addressId}                   -> data.status, data.taskStatus, data.reasons

Hosts: https://api.youverify.co (production), https://api.sandbox.youverify.co
(sandbox); auth header `token` (the API secret key). Same as the identity
adapter, whose helpers are reused.

What the docs do NOT settle (handled defensively):
- `taskStatus` values: the webhook page lists VERIFIED, NOT_VERIFIED and
  UNABLE_TO_VERIFY as "common values"; the spec's examples show "PENDING".
  The GET description says a check "may be pending, completed (verified/not
  verified), or canceled". Anything unrecognised is treated as still pending.
- Webhook signing for address events is not documented, so this adapter does
  not accept address webhooks; results are read with GET (polling).
"""
from __future__ import annotations

import logging

import requests

from app.services.verification.youverify_provider import TIMEOUT, _base_url, _secret

from .base import (AddressCheck, AddressOutcome, AddressRejected, AddressVerificationProvider,
                   ProviderUnavailable)

logger = logging.getLogger(__name__)

CANDIDATE_PATH = "/v2/api/addresses/candidates/identity"
REQUEST_PATH = "/v2/api/addresses/individual/request"
FETCH_PATH = "/v2/api/addresses/{address_id}"

_TASK_STATUS = {
    "VERIFIED": AddressOutcome.VERIFIED,
    "NOT_VERIFIED": AddressOutcome.NOT_VERIFIED,
    "UNABLE_TO_VERIFY": AddressOutcome.UNABLE_TO_VERIFY,
}
_REASON = {AddressOutcome.NOT_VERIFIED: "ADDRESS_NOT_CONFIRMED",
           AddressOutcome.UNABLE_TO_VERIFY: "AGENT_UNABLE",
           AddressOutcome.CANCELLED: "PROVIDER_CANCELLED"}


def check_from_data(data) -> AddressCheck | None:
    """Map a Youverify address record (request response, GET response or webhook `data`)."""
    if not isinstance(data, dict):
        return None
    ref = str(data.get("id") or "")
    task = str(data.get("taskStatus") or "").strip().upper()
    status = str(data.get("status") or "").strip().lower()
    if task in _TASK_STATUS:
        outcome = _TASK_STATUS[task]
    elif status in ("canceled", "cancelled") or task in ("CANCELED", "CANCELLED"):
        outcome = AddressOutcome.CANCELLED
    else:
        outcome = AddressOutcome.PENDING  # "pending"/"PENDING", "awaiting_qa", or anything undocumented
    return AddressCheck(provider_ref=ref, outcome=outcome, reason_code=_REASON.get(outcome))


def _clean(value):
    value = (value or "").strip() if isinstance(value, str) else value
    return value or None


class YouverifyAddressProvider(AddressVerificationProvider):
    name = "youverify"

    def _post(self, path, body):
        key = _secret()
        if not key:
            raise ProviderUnavailable("YOUVERIFY_SECRET_KEY is not set")
        try:
            res = requests.post(f"{_base_url()}{path}", json=body, headers={"token": key}, timeout=TIMEOUT)
        except requests.RequestException as e:
            raise ProviderUnavailable(type(e).__name__) from e
        try:
            payload = res.json()
        except ValueError as e:
            raise ProviderUnavailable(f"HTTP {res.status_code}, invalid JSON") from e
        if res.status_code in (400, 404, 422):
            raise AddressRejected(f"HTTP {res.status_code}")
        if res.status_code >= 400:
            raise ProviderUnavailable(f"HTTP {res.status_code}")
        return (payload or {}).get("data") or {}

    def request_check(self, *, user_id, nin, address):
        candidate = self._post(CANDIDATE_PATH, {"type": "nin", "idNumber": nin, "subjectConsent": True})
        candidate_id = candidate.get("id")
        if not candidate_id:
            raise ProviderUnavailable("no candidate id")
        addr = {
            "flatNumber": _clean(address.flat_number),
            "buildingName": _clean(address.building_name),
            "buildingNumber": _clean(address.building_number),
            "landmark": _clean(address.landmark),
            "street": _clean(address.street),
            "subStreet": _clean(address.sub_street),
            "state": _clean(address.state),
            "city": _clean(address.city),
            "lga": _clean(address.lga),
        }
        data = self._post(REQUEST_PATH, {
            "candidateId": str(candidate_id),
            "subjectConsent": True,
            "description": "Siiqo seller business address",
            "address": {k: v for k, v in addr.items() if v is not None},
            "metadata": {"siiqoUserId": str(user_id)},
        })
        check = check_from_data(data)
        if check is None or not check.provider_ref:
            raise ProviderUnavailable("no address request id")
        return check

    def fetch_check(self, provider_ref):
        key = _secret()
        if not provider_ref or not key:
            return None
        try:
            res = requests.get(f"{_base_url()}{FETCH_PATH.format(address_id=provider_ref)}",
                               headers={"token": key}, timeout=TIMEOUT)
            if res.status_code != 200:
                return None
            return check_from_data((res.json() or {}).get("data"))
        except (requests.RequestException, ValueError) as e:
            logger.warning("[YOUVERIFY] address fetch %s failed: %s", provider_ref, type(e).__name__)
            return None
