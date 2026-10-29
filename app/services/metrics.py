"""
Monthly metrics report (Phase 0B "re-baseline traction"). Read-only.

Numbers come from real records only: orders and escrow for money, the fee
ledger for Siiqo's revenue, platform_events for the funnel. Funnel and fee
figures exist only from the date the Phase 0B hooks were deployed.
"""
from datetime import datetime, timezone

from sqlalchemy import func

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
        "notes": [
            "GMV counts NGN payments only; other currencies are reported separately once FX rates are stored.",
            "Funnel and fee figures start from the Phase 0B deployment date.",
        ],
    }
