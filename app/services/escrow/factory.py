import os
from app.services.escrow.payscrow import PayscrowProvider
from app.services.escrow.paystack_provider import PaystackProvider


def get_escrow_provider(orders=None):
    """
    Returns the active escrow provider based on orders or global configuration.
    - If orders contains physical goods, returns PayscrowProvider.
    - If orders contains only digital or service goods, returns PaystackProvider.
    """
    if orders:
        if not isinstance(orders, list):
            orders = [orders]
        
        has_physical = False
        for order in orders:
            for item in order.items:
                product_type = (item.product.product_type if item.product else 'physical') or 'physical'
                if product_type == 'physical':
                    has_physical = True
                    break
            if has_physical:
                break
        
        if has_physical:
            return PayscrowProvider()
        else:
            return PaystackProvider()

    provider_name = os.environ.get("ACTIVE_ESCROW_PROVIDER", "payscrow").lower()

    if provider_name == "paystack":
        return PaystackProvider()

    # Default: Payscrow (used for Payment Links and legacy flow)
    return PayscrowProvider()
