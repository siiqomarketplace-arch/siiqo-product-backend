"""What really happened to a released order's payout, in one place.

Every released escrow must end in exactly one of:
  split        the gateway's verified payment proves the split paid the seller
  transfer     a tracked transfer the gateway confirmed successful
  held         the money is in the seller's withdrawable Siiqo balance
               (never sent, a transfer failed and was given back, or restored)
  manual       an admin recorded a payout a person sent by hand
Anything else (a payout recorded with no proof, a transfer still pending,
nothing recorded at all) is "needs a look". Used by the admin order view, the
seller's order view, admin "Payouts that need a look" and the daily check.
"""
import logging
import os
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from app.extensions import db

log = logging.getLogger(__name__)

SPLIT, TRANSFER, TRANSFER_PENDING, HELD, MANUAL, NEEDS_LOOK, NOT_RELEASED, CASH = (
    "split", "transfer", "transfer_pending", "held", "manual", "needs_look", "not_released", "cash")
OK_STATES = (SPLIT, TRANSFER, HELD, MANUAL, CASH)


def _utcnow():
    return datetime.now(timezone.utc)


def _aware(dt):
    if dt is None:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def recon_days() -> int:
    try:
        return max(1, int(os.environ.get("PAYOUT_RECON_DAYS", "45")))
    except ValueError:
        return 45


def _money(v) -> str:
    return f"₦{Decimal(str(v or 0)):,.2f}"


class PayoutContext:
    """Everything payout_state needs for a batch of orders, in a fixed number
    of queries (an order list can show hundreds of released orders)."""

    def __init__(self, pairs):
        from app.models.finance import Ledger
        from app.models.payouts import ManualPayout, PayoutTransfer
        from app.services.split_settlement import _subaccount_vendors
        pairs = [(o, e) for o, e in pairs if o is not None]
        order_ids = [o.id for o, _ in pairs]
        vendor_ids = {o.vendor_id for o, _ in pairs}
        self.manual, self.transfers, self.ledger = {}, {}, {}
        if not order_ids:
            self.subs = set()
            return
        for m in ManualPayout.query.filter(ManualPayout.order_id.in_(order_ids)):
            self.manual[m.order_id] = m
        for t in (PayoutTransfer.query.filter(PayoutTransfer.kind == "order",
                                              PayoutTransfer.order_id.in_(order_ids))
                  .order_by(PayoutTransfer.id.asc())):
            self.transfers[t.order_id] = t          # latest wins
        refs = set()
        for o, e in pairs:
            refs.update(_refs_for(o, e))
        for row in Ledger.query.filter(Ledger.vendor_id.in_(vendor_ids), Ledger.reference_id.in_(refs)):
            self.ledger.setdefault((row.vendor_id, row.transaction_type, row.reference_id), row)
        self.subs = _subaccount_vendors(vendor_ids)

    def entry(self, vendor_id, kind, refs):
        for r in refs:
            row = self.ledger.get((vendor_id, kind, r))
            if row is not None:
                return row
        return None


def _refs_for(order, escrow):
    from app.services.split_settlement import restore_reference
    txn = getattr(escrow, "transaction_number", None) if escrow is not None else None
    key = txn or order.id
    return {f"PAYOUT-{order.id}", f"PAYOUT-{txn}" if txn else f"PAYOUT-{order.id}",
            restore_reference(order.id), f"FX-HOLD-{key}", f"VERIFY-HOLD-{key}",
            txn or f"ORD-{order.id}", f"ORD-{order.id}"}


def _legacy_marker(order, escrow, gateway, subs) -> bool:
    if gateway == "flutterwave":
        return bool(escrow.payscrow_ref and str(escrow.payscrow_ref).startswith("SPLIT:"))
    if gateway == "paystack":
        return escrow.split_settled is True or (escrow.split_settled is None and order.vendor_id in subs)
    return False


def payout_state(order, escrow, ctx: PayoutContext | None = None) -> dict:
    """{state, label, seller_label, reference, transfer_status, reason}"""
    from app.models.escrow import EscrowStatus
    from app.services import payout_tracking as pt
    from app.services.disputes import gateway_for
    from app.services.split_settlement import is_proven, restore_reference

    def out(state, label, seller_label=None, **extra):
        d = {"state": state, "label": label, "seller_label": seller_label or label,
             "reference": None, "transfer_status": None, "reason": None}
        d.update(extra)
        return d

    if escrow is None or escrow.status != EscrowStatus.RELEASED:
        return out(NOT_RELEASED, "Not released yet", "Not paid out yet: the money is held until the "
                   "buyer receives the order.")
    if (order.payment_method or "").upper() == "POD":
        return out(CASH, "Paid in cash on delivery", "The buyer paid you in cash on delivery.")
    if ctx is None:
        ctx = PayoutContext([(order, escrow)])

    manual = ctx.manual.get(order.id)
    if manual is not None:
        return out(MANUAL, f"Manual payout (ref {manual.reference}, {manual.gateway}, "
                           f"{manual.paid_on.isoformat()}, {_money(manual.amount)})",
                   f"Paid to your bank by Siiqo (ref {manual.reference}).", reference=manual.reference)
    if is_proven(escrow):
        return out(SPLIT, "Paid by split (proven)", "Paid to your bank directly at checkout.")

    tr = ctx.transfers.get(order.id)
    if tr is not None:
        if tr.status == pt.SUCCESS:
            return out(TRANSFER, f"Paid by transfer (ref {tr.reference}, status successful)",
                       f"Sent to your bank (ref {tr.reference}).",
                       reference=tr.reference, transfer_status=tr.status)
        if tr.status == pt.PENDING:
            age = _utcnow() - (_aware(tr.created_at) or _utcnow())
            label = f"Paid by transfer (ref {tr.reference}, status pending)"
            if age > timedelta(hours=24):
                return out(NEEDS_LOOK, label + ", still pending after 24h",
                           f"On its way to your bank (ref {tr.reference}).",
                           reference=tr.reference, transfer_status=tr.status,
                           reason="Transfer still pending after 24h")
            return out(TRANSFER_PENDING, label, f"On its way to your bank (ref {tr.reference}).",
                       reference=tr.reference, transfer_status=tr.status)
        if tr.restored_at is not None:   # failed / reversed and given back
            return out(HELD, f"Held in seller balance (transfer {tr.reference} {tr.status}; money given back)",
                       "In your Siiqo balance: the bank transfer didn't go through. Withdraw it from Payouts.",
                       reference=tr.reference, transfer_status=tr.status)
        return out(NEEDS_LOOK, f"Needs a look (transfer {tr.reference} {tr.status}, not given back)",
                   "We're checking this payout.", reference=tr.reference, transfer_status=tr.status,
                   reason=f"Transfer {tr.status} and not restored")

    txn = escrow.transaction_number
    key = txn or order.id
    v = order.vendor_id
    debit = ctx.entry(v, "DEBIT", [f"PAYOUT-{txn}" if txn else f"PAYOUT-{order.id}", f"PAYOUT-{order.id}"])
    if debit is not None:
        if ctx.entry(v, "CREDIT", [restore_reference(order.id)]) is not None:
            return out(HELD, "Held in seller balance (payout restored by admin)",
                       "In your Siiqo balance. Withdraw it from Payouts.")
        if _legacy_marker(order, escrow, gateway_for(order, escrow), ctx.subs):
            return out(NEEDS_LOOK, "Needs a look (recorded as paid by split, no proof)",
                       "We're checking this payout.",
                       reason="Recorded as paid by split with no proof and no transfer")
        return out(TRANSFER, "Paid by transfer (sent before transfers were tracked; final status not recorded)",
                   "Sent to your bank.", transfer_status="untracked")

    if ctx.entry(v, "DEBIT", [f"FX-HOLD-{key}"]) is not None:
        return out(NEEDS_LOOK, "Held (currency conversion by support)",
                   "Held: our team will convert it and send your payout.",
                   reason="Held for currency conversion by support")
    if ctx.entry(v, "DEBIT", [f"VERIFY-HOLD-{key}"]) is not None:
        return out(HELD, "Held (waiting for the seller to verify)",
                   "Held until you verify your business. Then it's sent to you.")
    if ctx.entry(v, "CREDIT", [txn or f"ORD-{order.id}", f"ORD-{order.id}"]) is not None:
        return out(HELD, "Held in seller balance (not sent automatically: no bank account, name not confirmed, "
                         "or transfer refused)",
                   "In your Siiqo balance. Withdraw it from Payouts.")
    return out(NEEDS_LOOK, "Needs a look (released, nothing recorded)", "We're checking this payout.",
               reason="Released with no credit, transfer or payout record")


def payout_states(orders) -> dict:
    """{order_id: payout_state} for many orders in a fixed number of queries."""
    pairs = [(o, getattr(o, "escrow", None)) for o in orders]
    released = [(o, e) for o, e in pairs if e is not None and e.status == "RELEASED"]
    ctx = PayoutContext(released)
    return {o.id: payout_state(o, e, ctx) for o, e in pairs}


def payout_gaps(now=None, older_than_hours=24, limit=500) -> list:
    """Released escrows (released more than `older_than_hours` ago, within
    PAYOUT_RECON_DAYS) whose payout is not split / transfer / held / manual."""
    from app.models.escrow import EscrowStatus, EscrowTransaction
    from app.models.order import Order
    now = now or _utcnow()
    since = now - timedelta(days=recon_days())
    cutoff = now - timedelta(hours=older_than_hours)
    pairs = (db.session.query(EscrowTransaction, Order)
             .join(Order, Order.id == EscrowTransaction.order_id)
             .filter(EscrowTransaction.status == EscrowStatus.RELEASED,
                     EscrowTransaction.released_at >= since, EscrowTransaction.released_at < cutoff)
             .order_by(EscrowTransaction.released_at.desc()).limit(limit).all())
    gaps = []
    ctx = PayoutContext([(o, e) for e, o in pairs])
    for escrow, order in pairs:
        try:
            st = payout_state(order, escrow, ctx)
        except Exception as exc:  # noqa: BLE001
            st = {"state": NEEDS_LOOK, "label": "Needs a look", "reason": f"Could not check: {exc}"}
        if st["state"] != NEEDS_LOOK:
            continue   # split / transfer (incl. one sent < 24h ago) / held / manual / cash
        gaps.append({"kind": "payout_gap", "status": st["state"], "order_id": order.id,
                     "vendor_id": order.vendor_id, "amount_ngn": float(escrow.amount or 0),
                     "at": _aware(escrow.released_at), "reason": st.get("reason") or st["label"],
                     "label": st["label"]})
    return gaps


def run_daily_reconciliation(now=None) -> dict:
    """Daily: every released escrow must be split / transfer / held / manual.
    Each gap is logged at ERROR (Sentry alerts) and shows in admin
    "Payouts that need a look" (app/services/activation.payout_failures)."""
    gaps = payout_gaps(now=now)
    for g in gaps:
        log.error("[PAYOUT RECON] Order #%s (seller %s, ₦%s): %s", g["order_id"], g["vendor_id"],
                  g["amount_ngn"], g["reason"])
    log.info("[PAYOUT RECON] %d payout(s) need a look", len(gaps))
    return {"gaps": len(gaps), "order_ids": [g["order_id"] for g in gaps]}
