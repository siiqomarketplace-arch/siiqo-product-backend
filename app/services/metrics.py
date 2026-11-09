"""
Monthly metrics report (Phase 0B "re-baseline traction"). Read-only.

Numbers come from real records only: orders and escrow for money, the fee
ledger for Siiqo's revenue, platform_events for the funnel. Funnel and fee
figures exist only from the date the Phase 0B hooks were deployed.
"""
from datetime import datetime, timezone

from sqlalchemy import func, select

from app.extensions import db


def month_bounds(month: str):
    """'2026-10' -> (start, end) in UTC."""
    start = datetime.strptime(month, "%Y-%m").replace(tzinfo=timezone.utc)
    end = start.replace(year=start.year + 1, month=1) if start.month == 12 else start.replace(month=start.month + 1)
    return start, end


def monthly_metrics(month: str) -> dict:
    from app.models.admin import VendorSubscription
    from app.models.escrow import EscrowStatus, EscrowTransaction
    from app.models.foundations import FeeLedgerEntry
    from app.models.order import Order
    from app.models.telemetry import PlatformEvent
    from app.models.user import Storefront, User, UserRole

    start, end = month_bounds(month)

    def between(col):
        return (col >= start) & (col < end)

    paid_statuses = (EscrowStatus.IN_ESCROW, EscrowStatus.SHIPPED, EscrowStatus.DELIVERED,
                     EscrowStatus.RELEASED, EscrowStatus.DISPUTED)
    paid = (db.session.query(func.count(EscrowTransaction.id), func.coalesce(func.sum(EscrowTransaction.amount), 0))
            .filter(between(EscrowTransaction.paid_at), EscrowTransaction.status.in_(paid_statuses),
                    func.coalesce(EscrowTransaction.currency, 'NGN') == 'NGN')
            .one())
    released = EscrowTransaction.query.filter(between(EscrowTransaction.released_at),
                                              EscrowTransaction.status == EscrowStatus.RELEASED).count()
    refunded = EscrowTransaction.query.filter(between(EscrowTransaction.created_at),
                                              EscrowTransaction.status == EscrowStatus.REFUNDED).count()
    disputed = EscrowTransaction.query.filter(between(EscrowTransaction.created_at),
                                              EscrowTransaction.status == EscrowStatus.DISPUTED).count()

    selling = (db.session.query(func.count(func.distinct(Order.vendor_id)))
               .join(EscrowTransaction, EscrowTransaction.order_id == Order.id)
               .filter(between(EscrowTransaction.paid_at), EscrowTransaction.status.in_(paid_statuses))
               .scalar())

    fees = {row.event: float(row.total) for row in
            db.session.query(FeeLedgerEntry.event, func.coalesce(func.sum(FeeLedgerEntry.fee_amount), 0).label("total"))
            .filter(between(FeeLedgerEntry.created_at), FeeLedgerEntry.currency == 'NGN')
            .group_by(FeeLedgerEntry.event)}

    funnel = {row.event_name.replace("funnel.", ""): row.n for row in
              db.session.query(PlatformEvent.event_name, func.count(PlatformEvent.id).label("n"))
              .filter(between(PlatformEvent.timestamp), PlatformEvent.event_name.like("funnel.%"))
              .group_by(PlatformEvent.event_name)}

    now = datetime.now(timezone.utc)
    return {
        "month": month,
        "sellers": {
            "new_vendor_accounts": User.query.filter(between(User.created_at), User.role == UserRole.VENDOR).count(),
            "new_stores": Storefront.query.filter(between(Storefront.created_at)).count(),
            "stores_with_a_paid_order": int(selling or 0),
        },
        "orders": {
            "paid": int(paid[0]),
            "gmv_ngn": float(paid[1]),
            "released": released,
            "refunded": refunded,
            "disputed": disputed,
        },
        "revenue": {
            "fees_charged_ngn": fees.get("CHARGED", 0.0),
            "fees_reversed_ngn": fees.get("REVERSED", 0.0),
        },
        "verified_pro": {
            "active_subscriptions_now": VendorSubscription.query.filter(
                VendorSubscription.status.in_(("ACTIVE", "CANCELLED_PENDING_EXPIRY")),
                VendorSubscription.end_date > now).count(),
        },
        "funnel": funnel,
        "buying": buying_metrics(month),
        "notes": [
            "GMV counts NGN payments only; other currencies are reported separately once FX rates are stored.",
            "Funnel and fee figures start from the Phase 0B deployment date.",
        ],
    }


# ── Buying funnel (BUYING_FIX_PLAN A3) ──────────────────────────────────────
# Each step counts the new "funnel.<step>" name plus the older website name
# where one existed, so the report works across the switch-over.
BUYING_STEPS = (
    ("product_viewed", ("funnel.product_viewed", "product_viewed")),
    ("store_viewed", ("funnel.store_viewed", "store_viewed")),
    ("add_to_cart", ("funnel.add_to_cart", "add_to_cart")),
    ("checkout_opened", ("funnel.checkout_opened", "checkout_opened")),
    ("delivery_submitted", ("funnel.delivery_submitted",)),
    ("order_created", ("funnel.order_created",)),   # + legacy checkout_started, see below
    ("payment_started", ("funnel.payment_started",)),
    ("paid", ("funnel.paid",)),
    ("shipped", ("funnel.shipped",)),
    ("confirmed", ("funnel.confirmed",)),
    ("disputed", ("funnel.disputed",)),
)
NOT_SHIPPED_AFTER_HOURS = 72
_SHIPPED_OR_DONE = ("SHIPPED", "DELIVERED", "COMPLETED", "REFUNDED", "CANCELLED", "DISPUTED")


def _aware(dt):
    if dt is None:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def buying_metrics(month: str) -> dict:
    from app.models.order import Order
    from app.models.telemetry import PlatformEvent

    start, end = month_bounds(month)
    in_month = (PlatformEvent.timestamp >= start) & (PlatformEvent.timestamp < end)
    names = {n for _, group in BUYING_STEPS for n in group} | {"checkout_started"}
    raw = dict(db.session.query(PlatformEvent.event_name, func.count(PlatformEvent.id))
               .filter(in_month, PlatformEvent.event_name.in_(names))
               .group_by(PlatformEvent.event_name).all())
    counts = {step: sum(int(raw.get(n, 0)) for n in group) for step, group in BUYING_STEPS}

    # The cart also logs the older "checkout_started" for each order it creates;
    # count it only for orders without a funnel.order_created (before the switch).
    if raw.get("checkout_started"):
        has_new = select(PlatformEvent.order_id).where(PlatformEvent.event_name == "funnel.order_created",
                                                       PlatformEvent.order_id.isnot(None))
        counts["order_created"] += (db.session.query(func.count(PlatformEvent.id))
                                    .filter(in_month, PlatformEvent.event_name == "checkout_started",
                                            ~func.coalesce(PlatformEvent.order_id, -1).in_(has_new))
                                    .scalar() or 0)

    steps = [s for s, _ in BUYING_STEPS if s != "disputed"]
    conversion = {}
    for prev, cur in zip(steps, steps[1:]):
        conversion[f"{prev}_to_{cur}"] = (round(100.0 * counts[cur] / counts[prev], 1)
                                          if counts[prev] else None)

    paid_orders = (Order.query
                   .filter(Order.paid_at >= start, Order.paid_at < end,
                           func.upper(func.coalesce(Order.payment_method, "")) != "FREE")
                   .all())
    by_source, by_campaign = {}, {}
    for o in paid_orders:
        amount = float(o.total_amount or 0) if (o.currency or "NGN") == "NGN" else 0.0
        for bucket, key in ((by_source, o.source or "unknown"), (by_campaign, o.campaign or "none")):
            row = bucket.setdefault(key, {"paid_orders": 0, "gmv_ngn": 0.0})
            row["paid_orders"] += 1
            row["gmv_ngn"] = round(row["gmv_ngn"] + amount, 2)

    order_ids = [o.id for o in paid_orders]
    shipped_at = {}
    if order_ids:
        for oid, ts in (db.session.query(PlatformEvent.order_id, func.min(PlatformEvent.timestamp))
                        .filter(PlatformEvent.event_name == "funnel.shipped",
                                PlatformEvent.order_id.in_(order_ids))
                        .group_by(PlatformEvent.order_id)):
            shipped_at[oid] = _aware(ts)

    now = datetime.now(timezone.utc)
    hours, not_shipped = [], 0
    for o in paid_orders:
        paid = _aware(o.paid_at)
        shipped = shipped_at.get(o.id) or _aware(o.delivered_at)
        if shipped and paid and shipped >= paid:
            hours.append((shipped - paid).total_seconds() / 3600)
        elif (not shipped and o.released_at is None and (o.status or "").upper() not in _SHIPPED_OR_DONE
              and paid and (now - paid).total_seconds() > NOT_SHIPPED_AFTER_HOURS * 3600):
            not_shipped += 1
    hours.sort()
    median = None
    if hours:
        mid = len(hours) // 2
        median = round(hours[mid] if len(hours) % 2 else (hours[mid - 1] + hours[mid]) / 2, 1)

    return {
        "steps": counts,
        "conversion_pct": conversion,
        "paid_by_source": by_source,
        "paid_by_campaign": by_campaign,
        "median_hours_paid_to_shipped": median,
        "paid_not_shipped_after_72h": not_shipped,
        "notes": [
            "Website steps (views, cart, checkout, delivery, payment started) can be missed by ad blockers; "
            "orders, paid, shipped, confirmed and disputed come from the server.",
            "GMV by source and campaign counts NGN orders paid this month (goods only, no delivery fee).",
        ],
    }
