import os
from app.services.escrow.payscrow import PayscrowProvider
from app.services.escrow.paystack_provider import PaystackProvider


def get_escrow_provider(orders=None):
    """
    Returns the active escrow provider based on orders or global configuration.

    Architecture (July 2026):
    - Physical products  → ALWAYS paid via Daya (payment_method=CRYPTO).
                           They never go through /escrow/initiate, so this
                           factory is never called for them.
    - Digital products   → PaystackProvider (split payment via subaccount)
    - Service products   → PaystackProvider (split payment via subaccount)
    - PayScrow           → Retired. Code retained for legacy Payment Links only.

    If somehow called with physical orders, fall back to PaystackProvider
    (rather than PayscrowProvider) to avoid calling a retired provider.
    """
    if orders:
        if not isinstance(orders, list):
            orders = [orders]

        # Check if ALL items are digital or service
        all_non_physical = True
        for order in orders:
            for item in order.items:
                product_type = (item.product.product_type if item.product else 'physical') or 'physical'
                if product_type == 'physical':
                    all_non_physical = False
                    break
            if not all_non_physical:
                break

        # Both physical and digital/service use PaystackProvider for /escrow/initiate.
        # Physical products should be going through Daya (CRYPTO) and never reach here,
        # but if they do, PaystackProvider is the safe fallback (not PayscrowProvider).
        return PaystackProvider()

    # No orders provided — use env config (default Paystack)
    provider_name = os.environ.get("ACTIVE_ESCROW_PROVIDER", "paystack").lower()
    if provider_name == "payscrow":
        # Legacy Payment Links only — do not use for new marketplace orders
        return PayscrowProvider()
    return PaystackProvider()
