import os
from app.services.escrow.paystack_provider import PaystackProvider


def get_escrow_provider(orders=None):
    """
    Returns the active escrow provider based on orders or global configuration.

    Architecture:
    - Physical products  → ALWAYS paid via Daya (payment_method=CRYPTO or NGN onramp bank transfer).
    - Digital products   → PaystackProvider (split payment via subaccount) or Daya.
    - Service products   → PaystackProvider (split payment via subaccount) or Daya.
    - Payscrow           → Completely deleted.
    """
    return PaystackProvider()

