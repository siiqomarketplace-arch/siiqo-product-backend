"""One card payment, many orders.

The website (and bot) send every order of a cart in ONE card payment. Each
order keeps its own escrow with its own unique ``transaction_number``; the
gateway's reference (Paystack ``reference`` / Flutterwave ``tx_ref``) is stored
on every escrow of that payment in ``payment_ref``.

Everything that starts from a gateway reference (webhooks, status checks,
verify fallbacks, retries) uses ``escrows_for_payment`` so it finds ALL orders
of the payment. Older rows have no ``payment_ref``; they carried the gateway
reference in ``transaction_number`` and are still found that way.
"""
import os
from decimal import Decimal

from sqlalchemy import or_


def multi_split_enabled() -> bool:
    """MULTI_SPLIT_ENABLED (default false): split one card payment across
    several sellers' gateway subaccounts at checkout."""
    return (os.environ.get("MULTI_SPLIT_ENABLED") or "false").strip().lower() in ("1", "true", "yes", "on")


def seller_net_by_vendor(orders) -> dict:
    """{vendor_id: Decimal net} where each order's net is exactly what the
    release path would pay by transfer: order total minus Siiqo's fee
    (escrow.amount - escrow.fee_amount) plus the seller's own delivery fee
    (whole, no 3%; app/services/delivery_money.py). A delivery partner's fee
    stays with Siiqo, which pays the partner."""
    from app.services.delivery_money import seller_delivery_fee
    from app.services.fees import platform_fee
    out = {}
    for o in orders:
        total = Decimal(str(o.total_amount or 0))
        out[o.vendor_id] = (out.get(o.vendor_id, Decimal("0")) + (total - platform_fee(total))
                            + seller_delivery_fee(o))
    return out


def ngn_only(orders) -> bool:
    """A gateway split is only attempted for naira orders: other currencies
    go through the payout path, which converts or holds them."""
    return all((getattr(o, "currency", None) or "NGN").strip().upper() == "NGN" for o in orders)


def escrow_txn_number(payment_ref: str, order_id: int, order_count: int) -> str:
    """Per-escrow transaction_number. A single-order payment keeps the gateway
    reference itself (unchanged behaviour); a multi-order payment suffixes the
    order id so the UNIQUE column never clashes."""
    if order_count <= 1:
        return payment_ref
    return f"{payment_ref}-O{order_id}"


def gateway_ref(escrow) -> str | None:
    """The reference to ask the gateway about for this escrow."""
    return getattr(escrow, "payment_ref", None) or getattr(escrow, "transaction_number", None)


def escrows_for_payment(ref: str, order_ids=None) -> list:
    """Every escrow paid by the gateway payment ``ref`` (new rows by
    payment_ref, older rows by transaction_number). Optionally limited to
    ``order_ids``. Ordered by id so processing is stable."""
    from app.models.escrow import EscrowTransaction
    if not ref:
        return []
    q = EscrowTransaction.query.filter(or_(
        EscrowTransaction.payment_ref == ref,
        EscrowTransaction.transaction_number == ref,
    ))
    if order_ids:
        q = q.filter(EscrowTransaction.order_id.in_(list(order_ids)))
    return q.order_by(EscrowTransaction.id.asc()).all()


def reusable_payment_ref(escrows, order_ids) -> str | None:
    """Reference of an earlier attempt to pay exactly these orders, so a retry
    reuses it. Only when every order already has an escrow from the SAME
    payment; otherwise None (a fresh reference is safer than mixing two)."""
    by_order = {e.order_id: e for e in escrows}
    if set(by_order) != set(order_ids):
        return None
    refs = {gateway_ref(e) for e in escrows if e.payment_link}
    if len(refs) != 1 or len(escrows) != len([e for e in escrows if e.payment_link]):
        return None
    ref = refs.pop()
    if not ref:
        return None
    # The earlier payment must have covered these orders and no others.
    same = escrows_for_payment(ref)
    if {e.order_id for e in same} != set(order_ids):
        return None
    return ref
