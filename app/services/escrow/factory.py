import os
from app.services.escrow.paystack_provider import PaystackProvider


def get_escrow_provider(orders=None):
    """
    Returns the active escrow provider based on orders or global configuration.

    Architecture (as of 2026):
    - Physical products  → Daya (bank transfer/crypto via payment_method=DAYA_BANK_TRANSFER or CRYPTO)
    - Digital products   → Paystack (split payment via subaccount), Flutterwave, or Daya
    - Service products   → Paystack (split payment via subaccount), Flutterwave, or Daya
    - Event tickets      → Paystack, Flutterwave, or Daya
    - Payment Links      → Daya, Flutterwave, or Paystack (selected by link creator)
    
    NOTE: Payscrow completely removed. DB columns payscrow_ref and payscrow_transaction_id
          are kept for backward compatibility but now store generic provider references.
    """
    return PaystackProvider()

