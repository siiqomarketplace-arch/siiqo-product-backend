import os
from app.services.escrow.payscrow import PayscrowProvider
from app.services.escrow.paystack_provider import PaystackProvider


def get_escrow_provider():
    """
    Returns the active escrow provider based on ACTIVE_ESCROW_PROVIDER env var.

    Values:
        paystack  — marketplace checkout + subscriptions (default going forward)
        payscrow  — Payment Links / Siiqo Direct only (kept for /pay/[slug] flow)

    Set ACTIVE_ESCROW_PROVIDER=paystack on Elastic Beanstalk to activate.
    The .env local file still defaults to payscrow so existing local tests
    aren't disrupted until keys are added.
    """
    provider_name = os.environ.get("ACTIVE_ESCROW_PROVIDER", "payscrow").lower()

    if provider_name == "paystack":
        return PaystackProvider()

    # Default: Payscrow (used for Payment Links and legacy flow)
    return PayscrowProvider()
