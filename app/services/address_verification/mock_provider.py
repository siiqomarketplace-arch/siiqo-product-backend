"""
Mock address provider: lets the flow be built and tested without keys.
Never contacts the network.

A new check is PENDING. Its outcome, read by fetch_check, is chosen by the
LAST DIGIT of the building number, like provider sandboxes:

  9 -> NOT_VERIFIED
  8 -> UNABLE_TO_VERIFY
  7 -> stays PENDING
  anything else -> VERIFIED

Only the digit is kept in the reference; the address is not.
"""
from __future__ import annotations

import re
import uuid

from .base import AddressCheck, AddressOutcome, AddressVerificationProvider

_BY_DIGIT = {"9": AddressOutcome.NOT_VERIFIED, "8": AddressOutcome.UNABLE_TO_VERIFY,
             "7": AddressOutcome.PENDING}
_REASON = {AddressOutcome.NOT_VERIFIED: "ADDRESS_NOT_CONFIRMED",
           AddressOutcome.UNABLE_TO_VERIFY: "AGENT_UNABLE"}


class MockAddressProvider(AddressVerificationProvider):
    name = "mock"

    def request_check(self, *, user_id, nin, address):
        digits = re.sub(r"\D", "", address.building_number or "")
        digit = digits[-1:] or "0"
        return AddressCheck(provider_ref=f"mockaddr_{uuid.uuid4().hex[:12]}_{digit}",
                            outcome=AddressOutcome.PENDING)

    def fetch_check(self, provider_ref):
        if not provider_ref or not provider_ref.startswith("mockaddr_"):
            return None
        outcome = _BY_DIGIT.get(provider_ref.rsplit("_", 1)[-1], AddressOutcome.VERIFIED)
        return AddressCheck(provider_ref=provider_ref, outcome=outcome, reason_code=_REASON.get(outcome))
