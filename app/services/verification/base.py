"""
Provider interface for verification v2.

Every provider (mock today; Smile ID / Mono / Dojah / QoreID later) implements
this one interface, so the rest of Siiqo never depends on a specific vendor.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field


class Outcome:
    PASS = "PASS"
    RETRY = "RETRY"                # capture problem (blurry selfie, bad light): seller may retry
    MISMATCH = "MISMATCH"          # data does not match / ID not found: final
    FRAUD = "FRAUD"                # spoof or fraud signal: final, no refund
    INCONCLUSIVE = "INCONCLUSIVE"  # provider could not decide: route to support


@dataclass
class CheckResult:
    check_type: str          # nin_lookup | liveness | face_match | cac_lookup | director_match
    result: str              # PASS | FAIL | INCONCLUSIVE
    score: float | None = None
    threshold: float | None = None
    detail_code: str | None = None


@dataclass
class IdentitySession:
    provider_ref: str
    hosted_url: str


@dataclass
class IdentityResult:
    provider_ref: str
    outcome: str
    full_name: str | None = None
    reason_code: str | None = None
    checks: list[CheckResult] = field(default_factory=list)


@dataclass
class BusinessResult:
    outcome: str                      # PASS (found and active) | MISMATCH (not found) | INCONCLUSIVE
    business_name: str | None = None
    is_active: bool = False
    people: list[str] = field(default_factory=list)  # directors / proprietors / PSC names
    reason_code: str | None = None


class VerificationProvider(ABC):
    name: str = "base"

    @abstractmethod
    def start_identity(self, *, case_id: int, id_number: str, seller_name: str,
                       return_url: str) -> IdentitySession:
        """Start a hosted NIN + liveness + face-match session."""

    @abstractmethod
    def fetch_identity_result(self, provider_ref: str,
                              seller_name: str | None = None) -> IdentityResult | None:
        """Polling fallback: return the result if the provider has one, else None.
        seller_name is only used by the mock provider."""

    @abstractmethod
    def verify_webhook(self, raw_body: bytes, headers) -> bool:
        """Return True only if the webhook is authentically from the provider."""

    @abstractmethod
    def parse_identity_webhook(self, payload: dict) -> IdentityResult | None:
        """Turn a provider webhook payload into an IdentityResult (None if not identity)."""

    @abstractmethod
    def lookup_business(self, *, reg_number: str, business_type: str) -> BusinessResult:
        """Look up a CAC registration and return its status and people."""
