"""Read-only facts for the seller dashboard home (Phase 1, item 1-4).

The website picks the one "next best action" from these facts with fixed
rules (no AI). Nothing here writes to the database.
"""
from datetime import datetime, timedelta, timezone

from sqlalchemy import func

from app.extensions import db

# Unpaid orders older than this are treated as abandoned checkouts, not as
# something the seller can still act on.
UNPAID_ORDER_WINDOW_DAYS = 7

# Paid orders the seller still has to send or deliver.
TO_SHIP_STATUSES = ("PAID", "PROCESSING", "PENDING_DELIVERY")


def has_payout_account(vendor) -> bool:
    """Same test the Pay Link create route uses (no bank, no Pay Link)."""
    from app.models.withdrawal import VendorBankAccount

    if VendorBankAccount.query.filter_by(vendor_id=vendor.id).first() is not None:
        return True
    sf = vendor.storefront
    return bool(sf and sf.account_number and sf.bank_code)


def seller_home_facts(vendor, now=None) -> dict:
    from app.models.order import Order
    from app.models.payment_link import PaymentLink
    from app.models.product import Product
    from app.models.telemetry import PlatformEvent

    now = now or datetime.now(timezone.utc)
    sf = vendor.storefront

    product_count = 0
    if sf:
        product_count = Product.query.filter(
            Product.storefront_id == sf.id,
            Product.is_active.is_(True),
            db.or_(Product.is_deleted.is_(False), Product.is_deleted.is_(None)),
        ).count()

    # Deactivated links are marked EXPIRED (see DELETE /vendor/payment-links).
    link_ids = [row[0] for row in db.session.query(PaymentLink.id).filter(
        PaymentLink.vendor_id == vendor.id,
        PaymentLink.link_type == "PAY_LINK",
        PaymentLink.status != "EXPIRED",
    ).all()]

    # "Opened" = someone loaded the public Pay Link page (funnel event) or an
    # order came through it. Siiqo cannot see a share itself, so this is the
    # closest honest signal that a link has been shared.
    pay_link_opened = False
    if link_ids:
        pay_link_opened = db.session.query(PlatformEvent.id).filter(
            PlatformEvent.event_name == "funnel.paylink_viewed",
            PlatformEvent.business_id == vendor.id,
        ).first() is not None
        if not pay_link_opened:
            pay_link_opened = db.session.query(Order.id).filter(
                Order.payment_link_id.in_(link_ids)
            ).first() is not None

    since = (now - timedelta(days=UNPAID_ORDER_WINDOW_DAYS)).replace(tzinfo=None)
    unpaid_orders = db.session.query(func.count(Order.id)).filter(
        Order.vendor_id == vendor.id,
        Order.status == "PENDING",
        Order.created_at >= since,
    ).scalar() or 0

    orders_to_ship = db.session.query(func.count(Order.id)).filter(
        Order.vendor_id == vendor.id,
        Order.status.in_(TO_SHIP_STATUSES),
    ).scalar() or 0

    return {
        "has_storefront": sf is not None,
        "store_slug": sf.store_slug if sf else None,
        "product_count": int(product_count),
        "pay_link_count": len(link_ids),
        "latest_pay_link_slug": _latest_link_slug(vendor.id),
        "pay_link_opened": bool(pay_link_opened),
        "unpaid_orders": int(unpaid_orders),
        "unpaid_order_window_days": UNPAID_ORDER_WINDOW_DAYS,
        "orders_to_ship": int(orders_to_ship),
        "has_bank_account": has_payout_account(vendor),
    }


def _latest_link_slug(vendor_id):
    from app.models.payment_link import PaymentLink

    link = PaymentLink.query.filter(
        PaymentLink.vendor_id == vendor_id,
        PaymentLink.link_type == "PAY_LINK",
        PaymentLink.status != "EXPIRED",
    ).order_by(PaymentLink.created_at.desc()).first()
    return link.slug if link else None


def released_this_month(vendor_id, now=None) -> float:
    """Net amount (after Siiqo's fee) released to the seller since the 1st of
    this month, UTC."""
    from app.models.escrow import EscrowStatus, EscrowTransaction
    from app.models.order import Order

    now = now or datetime.now(timezone.utc)
    month_start = datetime(now.year, now.month, 1, tzinfo=timezone.utc)
    gross, fees = db.session.query(
        func.coalesce(func.sum(EscrowTransaction.amount), 0),
        func.coalesce(func.sum(EscrowTransaction.fee_amount), 0),
    ).join(Order, EscrowTransaction.order_id == Order.id).filter(
        Order.vendor_id == vendor_id,
        EscrowTransaction.status == EscrowStatus.RELEASED,
        EscrowTransaction.released_at >= month_start,
    ).one()
    return round(float(gross) - float(fees), 2)
