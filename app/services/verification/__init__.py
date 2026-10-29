"""Verification v2 provider selection."""
import os

from .base import VerificationProvider
from .mock_provider import MockProvider
from .youverify_provider import YouverifyProvider

_mock_singleton = MockProvider()
_youverify_singleton = YouverifyProvider()


def get_provider() -> VerificationProvider:
    """Return the configured provider: VERIFICATION_PROVIDER = mock (default) | youverify."""
    name = (os.environ.get("VERIFICATION_PROVIDER") or "mock").strip().lower()
    if name == "mock":
        return _mock_singleton
    if name == "youverify":
        return _youverify_singleton
    raise RuntimeError(f"Verification provider '{name}' is not implemented")
