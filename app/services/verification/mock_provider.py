"""
Mock verification provider — lets the whole flow be built and tested before
real provider keys exist. Never contacts the network.

Outcomes are chosen by the LAST DIGIT of the NIN / registration number, the
same way provider sandboxes work:

  Identity (NIN)        Business (RC/BN)
  9 -> MISMATCH         9 -> not found
  8 -> RETRY            8 -> found but inactive
  7 -> FRAUD            7 -> found, seller is not a director
  6 -> INCONCLUSIVE     anything else -> found, active, seller is a director
  5 -> PASS with a different legal name (tests name-mismatch handling)
  anything else -> PASS with the seller's own name
"""
from __future__ import annotations

import hashlib
import hmac
import os
import uuid

from .base import (BusinessResult, CheckResult, IdentityResult, IdentitySession, Outcome,
                   VerificationProvider)

_OUTCOME_BY_DIGIT = {"9": Outcome.MISMATCH, "8": Outcome.RETRY, "7": Outcome.FRAUD,
                     "6": Outcome.INCONCLUSIVE}


class MockProvider(VerificationProvider):
    name = "mock"

    def start_identity(self, *, case_id, id_number, seller_name, return_url):
        digit = id_number.strip()[-1:] or "0"
        # Encode what the mock needs into the reference so nothing sensitive is stored.
        ref = f"mock_{case_id}_{uuid.uuid4().hex[:8]}_{digit}"
        site = os.environ.get("SITE_URL", "https://siiqo.com").rstrip("/")
        return IdentitySession(provider_ref=ref,
                               hosted_url=f"{site}/vendor/trust/verify/mock?ref={ref}")

    def fetch_identity_result(self, provider_ref, seller_name=None):
        """seller_name is passed in by the service (from the database), so the mock
        holds no state between requests or workers."""
        if not provider_ref.startswith("mock_"):
            return None
        digit = provider_ref.rsplit("_", 1)[-1]
        outcome = _OUTCOME_BY_DIGIT.get(digit, Outcome.PASS)
        name = seller_name or "Mock Seller"
        if digit == "5":
            name = "Different Person Entirely"

        checks = [CheckResult("nin_lookup", "FAIL" if outcome == Outcome.MISMATCH else "PASS")]
        if outcome != Outcome.MISMATCH:
            checks.append(CheckResult("liveness", "FAIL" if outcome in (Outcome.RETRY, Outcome.FRAUD) else "PASS",
                                      detail_code="SPOOF_DETECTED" if outcome == Outcome.FRAUD else None))
            checks.append(CheckResult("face_match",
                                      "PASS" if outcome == Outcome.PASS else "INCONCLUSIVE"
                                      if outcome == Outcome.INCONCLUSIVE else "FAIL",
                                      score=92.0 if outcome == Outcome.PASS else 40.0, threshold=70.0))
        reason = {Outcome.MISMATCH: "ID_NOT_FOUND", Outcome.RETRY: "SELFIE_UNCLEAR",
                  Outcome.FRAUD: "SPOOF_DETECTED", Outcome.INCONCLUSIVE: "PROVIDER_INCONCLUSIVE"}.get(outcome)
        return IdentityResult(provider_ref=provider_ref, outcome=outcome,
                              full_name=name if outcome == Outcome.PASS else None,
                              reason_code=reason, checks=checks)

    def verify_webhook(self, raw_body, headers):
        secret = os.environ.get("VERIFICATION_WEBHOOK_SECRET", "")
        if not secret:
            return False
        expected = hmac.new(secret.encode(), raw_body, hashlib.sha256).hexdigest()
        return hmac.compare_digest(expected, headers.get("X-Siiqo-Mock-Signature", ""))

    def parse_identity_webhook(self, payload):
        ref = payload.get("provider_ref") or ""
        return self.fetch_identity_result(ref, payload.get("seller_name"))

    def lookup_business(self, *, reg_number, business_type):
        digit = reg_number.strip()[-1:] or "0"
        if digit == "9":
            return BusinessResult(outcome=Outcome.MISMATCH, reason_code="BUSINESS_NOT_FOUND")
        if digit == "8":
            return BusinessResult(outcome=Outcome.PASS, business_name="Mock Business Ltd",
                                  is_active=False, people=[], reason_code="BUSINESS_INACTIVE")
        people = ["Someone Else"] if digit == "7" else ["__SELLER__"]
        return BusinessResult(outcome=Outcome.PASS, business_name="Mock Business Ltd",
                              is_active=True, people=people)
