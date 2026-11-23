"""Where the buyer's delivery fee goes.

At checkout the server prices delivery (app/services/delivery.py) and stores it
on ``Order.logistics_fee``. The buyer pays items + delivery; Siiqo's 3% is on
the item total only and never on delivery.

    vendor_self        the seller delivers: the whole fee is the seller's,
                       paid with the order at release (no 3% on it)
    siiqo_partner_<id> a Siiqo delivery partner: the partner earns its own
                       price (LogisticsAssignment.delivery_fee); Siiqo keeps
                       the 10% markup (logistics_fee - partner price)
    pickup / none      no fee

The seller's delivery fee is a separate ledger CREDIT with the fixed
reference ``delivery_fee:<order_id>``. Release, the admin "Credit seller"
backfill and the refund rule all write that one reference, so it is credited
at most once whichever path runs first.

Refund rule (manual refunds, admin refund and dispute decisions):
  * not delivered -> the buyer gets item + delivery back; nobody earns delivery;
  * delivered, then refunded / decided for the buyer -> the buyer gets the item
    back only; the delivery fee goes to the partner (or to the seller for their
    own delivery).

Pay on Delivery: the buyer pays the seller in cash, so Siiqo never holds the
delivery money. POD orders can't use a delivery partner (checkout refuses it)
and never create Siiqo-paid delivery earnings.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal

from app.extensions import db

log = logging.getLogger(__name__)

CENT = Decimal("0.01")
VENDOR_SELF_ID = "vendor_self"
PARTNER_PREFIX = "siiqo_partner_"
PICKUP_IDS = ("pickup", "self_pickup")
REFUND_RULE_TEXT = ("Delivery fee refund rule: not delivered -> refund the item and the delivery fee; "
                    "delivered, then refunded -> refund the item only. The delivery fee goes to the "
                    "delivery partner, or to the seller for their own delivery.")


def _dec(value) -> Decimal:
    try:
        return Decimal(str(value if value is not None else "0")).quantize(CENT, rounding=ROUND_HALF_UP)
    except Exception:  # noqa: BLE001
        return Decimal("0.00")


def _now():
    return datetime.now(timezone.utc)


def _is_pod(order) -> bool:
    return (getattr(order, "payment_method", None) or "").upper() == "POD"


def naira(value) -> str:
    return f"₦{_dec(value):,.2f}"


# ── who delivers ───────────────────────────────────────────────────────────

def delivery_by(order) -> str | None:
    """'seller' | 'partner' | 'pickup' | None (no delivery option recorded)."""
    pid = (getattr(order, "logistics_provider_id", None) or "").strip()
    if pid == VENDOR_SELF_ID:
        return "seller"
    if pid.startswith(PARTNER_PREFIX):
        return "partner"
    if pid in PICKUP_IDS:
        return "pickup"
    if delivery_fee(order) > 0 and _assignment(order) is not None:
        return "partner"   # older rows without the option id
    return None


def delivery_fee(order) -> Decimal:
    return _dec(getattr(order, "logistics_fee", None))


def seller_delivery_fee(order) -> Decimal:
    """The delivery fee Siiqo owes the seller for their own delivery: the whole
    fee, no 3%. Zero for partner delivery, pickup, and Pay on Delivery (the
    seller collected it in cash)."""
    if order is None or _is_pod(order):
        return Decimal("0.00")
    if (getattr(order, "logistics_provider_id", None) or "").strip() != VENDOR_SELF_ID:
        return Decimal("0.00")
    fee = delivery_fee(order)
    return fee if fee > 0 else Decimal("0.00")


def seller_delivery_reference(order_id) -> str:
    return f"delivery_fee:{order_id}"


def seller_delivery_credited(order) -> bool:
    from app.models.finance import Ledger
    return Ledger.query.filter_by(
        vendor_id=order.vendor_id, transaction_type="CREDIT",
        reference_id=seller_delivery_reference(order.id)).first() is not None


def seller_delivery_share(order, escrow) -> Decimal:
    """The delivery part of the seller's payout for this order, in naira.

    Before release: the seller's own delivery fee (release credits it).
    After release: only when it was credited (``delivery_fee:<id>``). Orders
    released before this rule had no delivery credit, so a retried payout for
    them never pays the delivery fee retroactively; the admin backfill does."""
    fee = seller_delivery_fee(order)
    if fee <= 0:
        return Decimal("0.00")
    status = getattr(escrow, "status", None)
    if status in ("REFUNDED", "CANCELLED"):
        return Decimal("0.00")
    if status == "RELEASED":
        return fee if seller_delivery_credited(order) else Decimal("0.00")
    return fee


def credit_seller_delivery(order, description: str | None = None) -> bool:
    """Idempotent ledger CREDIT of the seller's own delivery fee. True when it
    was written now, False when there was nothing to credit or it already was.
    Does not commit."""
    fee = seller_delivery_fee(order)
    if fee <= 0 or seller_delivery_credited(order):
        return False
    from app.routes.escrow import _credit_vendor_ledger
    _credit_vendor_ledger(
        vendor_id=order.vendor_id, amount=float(fee),
        reference_id=seller_delivery_reference(order.id),
        description=description or f"Your delivery fee for Order #{order.id}",
    )
    return True


# ── payout breakdown shown to the seller ───────────────────────────────────

def payout_breakdown(order, escrow=None) -> dict:
    """Item − Siiqo 3% + your delivery = total, all in naira for an NGN order.
    ``text`` is the one-line form used in notices."""
    escrow = escrow if escrow is not None else getattr(order, "escrow", None)
    if escrow is not None:
        item = _dec(escrow.amount)
        fee = _dec(escrow.fee_amount or 0)
    else:
        from app.services.fees import platform_fee
        item = _dec(order.total_amount)
        fee = Decimal("0.00") if _is_pod(order) else platform_fee(item)
    delivery = seller_delivery_fee(order)
    total = item - fee + delivery
    text = f"Item {naira(item)} − Siiqo 3% {naira(fee)}"
    if delivery > 0:
        text += f" + your delivery {naira(delivery)}"
    text += f" = {naira(total)}"
    return {"item": str(item), "siiqo_fee": str(fee), "delivery": str(delivery),
            "total": str(total), "currency": "NGN", "text": text}


def buyer_breakdown(order) -> dict:
    """What the buyer paid: Item · Delivery (partner/seller) · Total."""
    item = _dec(order.total_amount)
    fee = delivery_fee(order)
    by = delivery_by(order)
    label = {"partner": "partner", "seller": "seller", "pickup": "pickup"}.get(by)
    total = item + fee
    text = f"Item {naira(item)} · Delivery {naira(fee)}" + (f" ({label})" if label and fee > 0 else "")
    text += f" · Total {naira(total)}"
    return {"item": str(item), "delivery": str(fee), "delivery_by": by, "total": str(total), "text": text}


# ── delivered? (refund rule) ───────────────────────────────────────────────

def _assignment(order):
    from app.models.escrow import LogisticsAssignment
    if order is None or getattr(order, "id", None) is None:
        return None
    return LogisticsAssignment.query.filter_by(order_id=order.id).first()


def was_delivered(order, escrow=None) -> bool:
    """A partner job counts as delivered only when the partner marked it
    DELIVERED. Otherwise (seller's own delivery, pickup): the seller or buyer
    marked the order delivered or completed, or the escrow had reached
    DELIVERED before a dispute froze it."""
    a = _assignment(order)
    if a is not None:
        return a.status == "DELIVERED"
    if getattr(order, "delivered_at", None) is not None:
        return True
    if (order.status or "").upper() in ("DELIVERED", "COMPLETED"):
        return True
    escrow = escrow if escrow is not None else getattr(order, "escrow", None)
    if escrow is not None and escrow.status in ("DELIVERED", "RELEASED"):
        return True
    try:
        from app.models.dispute import DisputeCase
        case = (DisputeCase.query.filter_by(order_id=order.id)
                .order_by(DisputeCase.id.desc()).first())
        if case is not None and (case.pre_dispute_escrow_status or "").upper() == "DELIVERED":
            return True
    except Exception:  # noqa: BLE001
        pass
    return False


def refund_split(order, escrow=None) -> dict:
    """How the delivery fee is treated when the buyer is refunded."""
    fee = delivery_fee(order)
    by = delivery_by(order)
    if fee <= 0 or _is_pod(order):
        return {"delivery_fee": str(fee), "delivered": was_delivered(order, escrow), "delivery_by": by,
                "buyer_delivery_refund": "0.00", "delivery_fee_to": None, "rule": REFUND_RULE_TEXT}
    delivered = was_delivered(order, escrow)
    if not delivered:
        to = "buyer"
        buyer_refund = fee
    else:
        to = "seller" if by == "seller" else "partner"
        buyer_refund = Decimal("0.00")
    return {"delivery_fee": str(fee), "delivered": delivered, "delivery_by": by,
            "buyer_delivery_refund": str(buyer_refund), "delivery_fee_to": to, "rule": REFUND_RULE_TEXT}


def apply_refund_rule(order, escrow, item_refund) -> dict:
    """Record the refund amounts on the escrow and pay the delivery fee where
    the rule sends it. Seller's own delivery, delivered: idempotent
    ``delivery_fee:<id>`` credit (not when a gateway split already paid the
    seller at checkout). Partner: earned via partner_earnings (DELIVERED job,
    escrow REFUNDED). Does not commit."""
    split = refund_split(order, escrow)
    delivery_refund = _dec(split["buyer_delivery_refund"])
    escrow.buyer_refund_amount = _dec(item_refund) + delivery_refund
    escrow.delivery_refund_ngn = delivery_refund
    escrow.delivery_fee_to = split["delivery_fee_to"]
    credited = False
    if split["delivery_fee_to"] == "seller":
        from app.services.disputes import seller_settled_at_checkout
        if not seller_settled_at_checkout(order, escrow):
            credited = credit_seller_delivery(
                order, f"Your delivery fee for Order #{order.id} (delivered before the refund)")
    split["seller_credited"] = credited
    split["item_refund"] = str(_dec(item_refund))
    split["buyer_refund_total"] = str(escrow.buyer_refund_amount)
    return split


# ── admin: seller delivery fees not paid (orders released before the fix) ──

def unpaid_seller_delivery_fees() -> list:
    """Orders whose escrow was RELEASED with the seller's own delivery fee
    (> 0, not POD) and no ``delivery_fee:<id>`` credit."""
    from app.models.escrow import EscrowTransaction
    from app.models.finance import Ledger
    from app.models.order import Order
    from app.models.user import Storefront
    rows = (db.session.query(Order, EscrowTransaction)
            .join(EscrowTransaction, EscrowTransaction.order_id == Order.id)
            .filter(Order.logistics_provider_id == VENDOR_SELF_ID,
                    Order.logistics_fee > 0,
                    EscrowTransaction.status == "RELEASED")
            .order_by(Order.id.asc()).all())
    out = []
    for order, escrow in rows:
        if _is_pod(order):
            continue
        credited = Ledger.query.filter_by(vendor_id=order.vendor_id, transaction_type="CREDIT",
                                          reference_id=seller_delivery_reference(order.id)).first()
        if credited is not None:
            continue
        sf = Storefront.query.filter_by(vendor_id=order.vendor_id).first()
        released = escrow.released_at or order.released_at
        out.append({
            "order_id": order.id,
            "vendor": {"id": order.vendor_id, "store_name": sf.store_name if sf else None},
            "delivery_fee": str(delivery_fee(order)),
            "released_at": released.isoformat() if released else None,
            "reference": seller_delivery_reference(order.id),
        })
    return out


def credit_unpaid_seller_delivery(order_id: int):
    """One-tap admin backfill. Returns (row, created) or (None, False) when the
    order is not on the list. Idempotent: the fixed reference is written once;
    the escrow row lock serialises two taps."""
    from app.models.communication import Notification
    from app.models.escrow import EscrowTransaction
    from app.models.order import Order
    from app.routes.escrow import lock_escrow
    order = db.session.get(Order, order_id)
    escrow = EscrowTransaction.query.filter_by(order_id=order_id).first() if order else None
    if order is None or escrow is None:
        return None, False
    escrow = lock_escrow(escrow)
    fee = seller_delivery_fee(order)
    if fee <= 0 or escrow.status != "RELEASED":
        db.session.rollback()
        return None, False
    row = {"order_id": order.id, "vendor_id": order.vendor_id, "delivery_fee": str(fee),
           "reference": seller_delivery_reference(order.id)}
    if not credit_seller_delivery(order, f"Your delivery fee for Order #{order.id} (paid by Siiqo support)"):
        db.session.rollback()
        return row, False
    db.session.add(Notification(
        user_id=order.vendor_id, title="Delivery fee added to your balance", type="ESCROW", order_id=order.id,
        message=(f"We added {naira(fee)}, your delivery fee for Order #{order.id}, to your Siiqo balance. "
                 "Withdraw it from Payouts."),
    ))
    return row, True


# ── admin: delivery money report ───────────────────────────────────────────

def _aware(dt):
    if dt is None:
        return None
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt


def _in(dt, start, end) -> bool:
    dt = _aware(dt)
    return dt is not None and start <= dt < end


def parse_period(start_raw=None, end_raw=None, days_default=30):
    """(start, end) as aware datetimes; end exclusive. Dates are YYYY-MM-DD."""
    def _parse(raw):
        if not raw:
            return None
        try:
            return datetime.strptime(str(raw)[:10], "%Y-%m-%d").replace(tzinfo=timezone.utc)
        except ValueError:
            return None
    end = _parse(end_raw)
    end = (end + timedelta(days=1)) if end else _now()
    start = _parse(start_raw) or (end - timedelta(days=days_default))
    if start > end:
        start, end = end, start
    return start, end


def delivery_money_report(start, end) -> dict:
    """Totals for orders paid in [start, end) plus balances as of now."""
    from app.models.escrow import LogisticsAssignment
    from app.models.order import Order
    from app.models.user import User
    from app.models.withdrawal import PartnerWithdrawal
    from app.services.logistics_flow import partner_earnings
    from app.services.logistics_partners import latest_application

    zero = Decimal("0.00")
    collected = partner_collected = seller_collected = margin = zero
    owed_sellers_held = zero
    paid_orders = 0
    orders = Order.query.filter(Order.logistics_fee > 0, Order.paid_at.isnot(None)).all()
    for o in orders:
        if _is_pod(o) or not _in(o.paid_at, start, end):
            continue
        fee = delivery_fee(o)
        paid_orders += 1
        collected += fee
        by = delivery_by(o)
        if by == "seller":
            seller_collected += fee
        elif by == "partner":
            partner_collected += fee
            a = _assignment(o)
            if a is not None:
                margin += fee - _dec(a.delivery_fee)

    # Sellers' own-delivery fees Siiqo still holds (paid, not released or refunded).
    for o in Order.query.filter(Order.logistics_provider_id == VENDOR_SELF_ID, Order.logistics_fee > 0,
                                Order.paid_at.isnot(None)).all():
        e = o.escrow
        if _is_pod(o) or e is None or e.status in ("RELEASED", "REFUNDED", "CANCELLED"):
            continue
        owed_sellers_held += delivery_fee(o)
    unpaid_released = unpaid_seller_delivery_fees()
    owed_sellers_unpaid = sum((_dec(r["delivery_fee"]) for r in unpaid_released), zero)

    paid_partners = zero
    for w in PartnerWithdrawal.query.filter(PartnerWithdrawal.status.in_(("PENDING", "COMPLETED"))).all():
        if _in(w.processed_at or w.requested_at, start, end):
            paid_partners += _dec(w.amount)

    partner_ids = sorted({pid for (pid,) in db.session.query(LogisticsAssignment.partner_id).distinct()}
                         | {pid for (pid,) in db.session.query(PartnerWithdrawal.partner_id).distinct()})
    partners = []
    owed_partners = zero
    for pid in partner_ids:
        money = partner_earnings(pid)
        user = db.session.get(User, pid)
        app_row = latest_application(pid)
        pod_jobs = (LogisticsAssignment.query.join(Order, Order.id == LogisticsAssignment.order_id)
                    .filter(LogisticsAssignment.partner_id == pid, Order.payment_method == "POD").count())
        paid_in_period = sum((_dec(w.amount) for w in PartnerWithdrawal.query.filter(
            PartnerWithdrawal.partner_id == pid,
            PartnerWithdrawal.status.in_(("PENDING", "COMPLETED"))).all()
            if _in(w.processed_at or w.requested_at, start, end)), zero)
        owed_partners += money["available"]
        partners.append({
            "partner_id": pid,
            "name": (app_row.business_name if app_row and app_row.business_name
                     else (user.full_name if user else f"Partner {pid}")),
            "status": (app_row.status if app_row else None),
            "completed_deliveries": money["completed_deliveries"],
            "earned": str(money["earned"]),
            "pending": str(money["pending"]),
            "withdrawn": str(money["withdrawn"]),
            "available": str(money["available"]),
            "paid_in_period": str(paid_in_period),
            "pod_jobs_legacy": pod_jobs,
        })

    pod_partner_jobs = (LogisticsAssignment.query.join(Order, Order.id == LogisticsAssignment.order_id)
                        .filter(Order.payment_method == "POD").count())
    pod_partner_fees = sum((_dec(a.delivery_fee) for a in LogisticsAssignment.query.join(
        Order, Order.id == LogisticsAssignment.order_id).filter(Order.payment_method == "POD").all()), zero)

    return {
        "period": {"from": start.date().isoformat(), "to": (end - timedelta(seconds=1)).date().isoformat()},
        "totals": {
            "orders_with_delivery": paid_orders,
            "collected_from_buyers": str(collected),
            "collected_for_partners": str(partner_collected),
            "collected_for_sellers": str(seller_collected),
            "owed_to_partners": str(owed_partners),
            "paid_to_partners": str(paid_partners),
            "owed_to_sellers_held": str(owed_sellers_held),
            "owed_to_sellers_unpaid_released": str(owed_sellers_unpaid),
            "siiqo_delivery_margin": str(margin),
        },
        "notes": {
            "period_totals": "Collected and margin: orders paid in the period. Paid to partners: withdrawals "
                             "sent in the period. Owed figures are balances as of now.",
            "margin": "Siiqo's delivery margin is the 10% added to the partner's price (buyer pays partner "
                      "price × 1.10; the partner earns its price).",
            "no_fee_on_delivery": "Siiqo's 3% is on the item total only. Delivery is never charged the 3%.",
        },
        "pod_partner_jobs_legacy": {"count": pod_partner_jobs, "fees": str(pod_partner_fees),
                                    "note": "Pay on Delivery jobs with a delivery partner made before checkout "
                                            "refused them. Siiqo never held this cash, so they are not counted "
                                            "as Siiqo-paid partner earnings. Settle any with the seller directly."},
        "unpaid_seller_delivery_count": len(unpaid_released),
        "partners": partners,
    }
