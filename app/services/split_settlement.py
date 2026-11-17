"""Was the seller already paid by a gateway split at checkout?

Paystack splits a payment to sellers' subaccounts when the checkout belongs
to ONE seller, or — with MULTI_SPLIT_ENABLED and every seller having a
subaccount — through a multi-split (paystack_provider.initiate_transaction
returns ``used_split``). A seller having a subaccount is therefore not proof
they were paid: without a split Siiqo must send their share itself (or keep
it in their withdrawable balance).

``escrow_transactions.split_settled`` records what really happened, set when
the Paystack payment is initialised:
  True  -> Paystack split this payment to the seller; nothing more to send.
  False -> no split; Siiqo pays the seller (transfer, else balance).
  NULL  -> older row, made before the flag existed. Classified from the
           payment it belonged to: escrows sharing the payment's reference.
           Two or more sellers -> no split was possible -> False.
           One seller -> the old rule (seller has a Paystack subaccount).

Flutterwave marks a split payment with a "SPLIT:" payscrow_ref; that marker
is per escrow and is used as-is.

Every release path asks ``vendor_paid_by_split`` and nothing else.
"""
import logging
import re
from decimal import Decimal

from sqlalchemy import or_

from app.extensions import db


def _refs(escrow) -> set:
    """Identifiers of the gateway payment this escrow belonged to.
    payment_ref is the reliable one (set on every escrow of a payment);
    the others cover rows made before it existed."""
    payment_ref = getattr(escrow, "payment_ref", None)
    if payment_ref:
        return {str(payment_ref)}
    refs = set()
    for value in (escrow.payscrow_ref, escrow.transaction_number, escrow.payscrow_transaction_id):
        if value:
            v = str(value)
            if v.startswith("SPLIT:"):
                v = v[len("SPLIT:"):]
            refs.add(v)
    return refs


def transaction_vendor_ids(escrow, order=None) -> set:
    """Sellers whose orders were paid in the same gateway payment as this escrow."""
    from app.models.escrow import EscrowTransaction
    from app.models.order import Order

    vendor_ids = set()
    if order is not None and order.vendor_id:
        vendor_ids.add(order.vendor_id)
    refs = _refs(escrow)
    if not refs:
        return vendor_ids
    rows = (
        db.session.query(Order.vendor_id)
        .join(EscrowTransaction, EscrowTransaction.order_id == Order.id)
        .filter(or_(
            EscrowTransaction.payment_ref.in_(refs),
            EscrowTransaction.transaction_number.in_(refs),
            EscrowTransaction.payscrow_ref.in_(refs),
            EscrowTransaction.payscrow_transaction_id.in_(refs),
        ))
        .all()
    )
    vendor_ids.update(r[0] for r in rows if r[0])
    return vendor_ids


def vendor_has_paystack_subaccount(vendor_id) -> bool:
    try:
        from app.models.user import Storefront
        from app.models.withdrawal import VendorBankAccount
        acc = (VendorBankAccount.query.filter_by(vendor_id=vendor_id, is_default=True).first()
               or VendorBankAccount.query.filter_by(vendor_id=vendor_id).first())
        if acc and acc.paystack_subaccount_code:
            return True
        sf = Storefront.query.filter_by(vendor_id=vendor_id).first()
        return bool(sf and sf.paystack_subaccount_code)
    except Exception as exc:  # noqa: BLE001
        logging.warning(f"[SPLIT] Subaccount lookup failed for vendor {vendor_id}: {exc}")
        return False


def paystack_split_settled(order, escrow) -> bool:
    flag = getattr(escrow, "split_settled", None)
    if flag is not None:
        return bool(flag)
    # Older row: a payment with several sellers was never split.
    if len(transaction_vendor_ids(escrow, order)) > 1:
        return False
    return vendor_has_paystack_subaccount(order.vendor_id)


def flutterwave_split_settled(escrow) -> bool:
    return bool(escrow.payscrow_ref and str(escrow.payscrow_ref).startswith("SPLIT:"))


def vendor_paid_by_split(order, escrow, gateway: str) -> bool:
    """True only when the gateway already paid this seller at checkout."""
    if gateway == "flutterwave":
        return flutterwave_split_settled(escrow)
    if gateway == "paystack":
        return paystack_split_settled(order, escrow)
    return False


# ── Past damage: payouts recorded as "paid by split" that could not have been ──

_ORDER_RE = re.compile(r"Order #(\d+)")
PAYOUT_DESC = "Automated gateway payout settlement"


def restore_reference(order_id: int) -> str:
    return f"RESTORE-PAYOUT-{order_id}"


def split_mismatches() -> list:
    """Orders where a payout DEBIT ("PAYOUT-…") was written for a Paystack
    order whose payment had two or more sellers, so no split could have paid
    them. Only sellers who have a Paystack subaccount are listed: the old code
    skipped the transfer for exactly those; for the others it debited only
    after a real transfer succeeded."""
    from app.models.escrow import EscrowTransaction
    from app.models.finance import Ledger
    from app.models.order import Order
    from app.models.user import Storefront
    from app.services.disputes import gateway_for

    debits = (
        Ledger.query.filter(Ledger.transaction_type == "DEBIT")
        .filter(or_(Ledger.reference_id.like("PAYOUT-%"), Ledger.description.like(f"{PAYOUT_DESC}%")))
        .order_by(Ledger.id.asc())
        .all()
    )
    out, seen = [], set()
    for d in debits:
        m = _ORDER_RE.search(d.description or "")
        if not m:
            continue
        order_id = int(m.group(1))
        if order_id in seen:
            continue
        order = db.session.get(Order, order_id)
        if order is None or order.vendor_id != d.vendor_id:
            continue
        escrow = EscrowTransaction.query.filter_by(order_id=order_id).first()
        if escrow is None or gateway_for(order, escrow) != "paystack":
            continue
        if escrow.split_settled is True:
            continue
        if len(transaction_vendor_ids(escrow, order)) < 2:
            continue
        if not vendor_has_paystack_subaccount(order.vendor_id):
            continue
        seen.add(order_id)
        restored = Ledger.query.filter_by(vendor_id=order.vendor_id, transaction_type="CREDIT",
                                          reference_id=restore_reference(order_id)).first()
        sf = Storefront.query.filter_by(vendor_id=order.vendor_id).first()
        out.append({
            "order_id": order_id,
            "vendor": {"id": order.vendor_id, "store_name": sf.store_name if sf else None},
            "amount": str(Decimal(str(d.amount)).quantize(Decimal("0.01"))),
            "paid_at": d.created_at.isoformat() if getattr(d, "created_at", None) else None,
            "transaction_ref": escrow.payscrow_ref or escrow.transaction_number,
            "debit_reference": d.reference_id,
            "restored": restored is not None,
        })
    return out


def restore_mismatch(order_id: int):
    """Write the compensating CREDIT for one listed order. Returns
    (row, created) or (None, False) when the order is not a mismatch.
    Idempotent: a second call finds the RESTORE- credit and writes nothing."""
    from app.models.communication import Notification
    from app.models.finance import Ledger
    from app.routes.escrow import _credit_vendor_ledger

    row = next((r for r in split_mismatches() if r["order_id"] == order_id), None)
    if row is None:
        return None, False
    if row["restored"]:
        return row, False
    amount = float(row["amount"])
    vendor_id = row["vendor"]["id"]
    _credit_vendor_ledger(
        vendor_id=vendor_id,
        amount=amount,
        reference_id=restore_reference(order_id),
        description=f"Restored payout for Order #{order_id} (no split was paid)",
    )
    db.session.add(Notification(
        user_id=vendor_id,
        title="Money added to your balance",
        message=(f"₦{amount:,.2f} from Order #{order_id} is now in your Siiqo balance. "
                 "You can withdraw it."),
        type="ESCROW",
        order_id=order_id,
    ))
    row = dict(row, restored=True)
    return row, True


def mismatch_total(rows) -> str:
    total = sum((Decimal(r["amount"]) for r in rows if not r.get("restored")), Decimal("0"))
    return str(total.quantize(Decimal("0.01")))
