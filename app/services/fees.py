"""
Siiqo's platform fee — the single source of truth (decision D1).

Siiqo charges one fee: 3% of the order subtotal (item total, excluding delivery
and gateway processing fees), on every rail. Pay on Delivery is the only
exception and records no fee (decision D9); that is handled by the POD path,
not here.
"""
from decimal import ROUND_HALF_UP, Decimal

PLATFORM_FEE_PERCENT = Decimal("3.00")
_CENT = Decimal("0.01")


def platform_fee(subtotal) -> Decimal:
    """Siiqo's fee on a subtotal, rounded to the kobo/cent."""
    amount = Decimal(str(subtotal or 0))
    return (amount * PLATFORM_FEE_PERCENT / Decimal("100")).quantize(_CENT, rounding=ROUND_HALF_UP)


def platform_fee_float(subtotal) -> float:
    """Same as platform_fee, for call sites that still do float arithmetic."""
    return float(platform_fee(subtotal))
