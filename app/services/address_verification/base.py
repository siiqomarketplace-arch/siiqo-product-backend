"""
Provider interface for address verification (V5).

Every provider (mock today, Youverify when switched on) implements this one
interface, so the rest of Siiqo never depends on a vendor. Providers receive
the address and ID number to do the check; Siiqo stores only the outcome,
dates and the provider's reference.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass


class AddressOutcome:
    PENDING = "PENDING"
    VERIFIED = "VERIFIED"
    NOT_VERIFIED = "NOT_VERIFIED"
    UNABLE_TO_VERIFY = "UNABLE_TO_VERIFY"
    CANCELLED = "CANCELLED"


@dataclass
class AddressInput:
    """A Nigerian street address as the seller typed it (never stored by Siiqo)."""
    building_number: str
    street: str
    landmark: str
    city: str
    state: str
    lga: str | None = None
    flat_number: str | None = None
    building_name: str | None = None
    sub_street: str | None = None


@dataclass
class AddressCheck:
    provider_ref: str
    outcome: str                 # an AddressOutcome value
    reason_code: str | None = None


class ProviderUnavailable(Exception):
    """The provider couldn't be reached or refused the request; nothing was decided."""


class AddressRejected(Exception):
    """The provider refused the input (e.g. NIN not found, invalid address); the seller can fix and retry."""


class AddressVerificationProvider(ABC):
    name: str = "base"

    @abstractmethod
    def request_check(self, *, user_id: int, nin: str, address: AddressInput) -> AddressCheck:
        """Start a check. Usually returns PENDING (a field agent visits later).
        The caller has already recorded the seller's consent."""

    @abstractmethod
    def fetch_check(self, provider_ref: str) -> AddressCheck | None:
        """Current state of a check, or None if the provider can't say right now."""
