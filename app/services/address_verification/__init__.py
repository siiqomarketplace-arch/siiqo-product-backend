"""Address verification (V5) provider selection."""
import os

from .base import AddressVerificationProvider
from .mock_provider import MockAddressProvider
from .youverify_provider import YouverifyAddressProvider

_mock = MockAddressProvider()
_youverify = YouverifyAddressProvider()


def get_provider() -> AddressVerificationProvider:
    """ADDRESS_VERIFICATION_PROVIDER = mock (default) | youverify."""
    name = (os.environ.get("ADDRESS_VERIFICATION_PROVIDER") or "mock").strip().lower()
    if name == "mock":
        return _mock
    if name == "youverify":
        return _youverify
    raise RuntimeError(f"Address verification provider '{name}' is not implemented")
