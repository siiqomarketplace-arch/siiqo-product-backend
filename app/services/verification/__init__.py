"""Verification v2 provider selection."""
import os

from .base import VerificationProvider
from .mock_provider import MockProvider

_mock_singleton = MockProvider()


def get_provider() -> VerificationProvider:
    """Return the configured provider. Only "mock" exists until real keys arrive."""
    name = (os.environ.get("VERIFICATION_PROVIDER") or "mock").strip().lower()
    if name == "mock":
        return _mock_singleton
    raise RuntimeError(f"Verification provider '{name}' is not implemented yet")
