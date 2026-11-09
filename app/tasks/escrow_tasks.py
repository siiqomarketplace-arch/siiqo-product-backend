"""
escrow_tasks.py — Background tasks for escrow management
Handles auto-release, dispute timeouts, and payment reminders
"""
import logging
logger = logging.getLogger(__name__)
from datetime import datetime, timezone, timedelta
from app.extensions import db
from app.models.escrow import EscrowTransaction, EscrowStatus
from app.models.order import Order
from app.models.finance import Ledger, Receipt
from app.models.communication import Notification


def utcnow():
    return datetime.now(timezone.utc)


def _credit_vendor_ledger(vendor_id: int, amount: float, reference_id: str, description: str):
    """Write a CREDIT entry to the vendor's ledger."""
    from sqlalchemy import func
    from app.models.finance import Ledger as L

    credits = db.session.query(func.sum(L.amount)).filter_by(
        vendor_id=vendor_id, transaction_type='CREDIT'
    ).scalar() or 0
    debits = db.session.query(func.sum(L.amount)).filter_by(
        vendor_id=vendor_id, transaction_type='DEBIT'
    ).scalar() or 0
    balance_after = float(credits) - float(debits) + amount

    db.session.add(Ledger(
        vendor_id=vendor_id,
        transaction_type='CREDIT',
        amount=amount,
        description=description,
        reference_id=reference_id,
        balance_after=balance_after,
    ))


def auto_release_escrow():
    """
    Release Safe Pay funds when the buyer neither confirmed nor reported a
    problem within the one public window (AUTO_RELEASE_HOURS, default 72h)
    after the order was marked delivered (app/services/trust.py).
    Runs every hour.
    """
    logger.info(f"[{utcnow()}] Running auto-release escrow task...")

    # Dispute deadlines ride on this hourly task: a case whose answering
    # window has passed goes to review (app/services/disputes.py).
    try:
        from app.services.disputes import sweep_deadlines
        moved = sweep_deadlines()
        if moved:
            logger.info(f"  [DISPUTES] {moved} case(s) moved to review after their deadline")
    except Exception as e:
        db.session.rollback()
        logger.warning(f"  [DISPUTES] deadline sweep failed: {e}")

    # Find all delivered escrow transactions and filter dynamically in python
    delivered_escrows = EscrowTransaction.query.filter(
        EscrowTransaction.status == EscrowStatus.DELIVERED
    ).all()

    escrows_to_release = []
    for escrow in delivered_escrows:
        order = escrow.order
        if not order:
            continue

        from app.services.trust import release_deadline
        deadline = release_deadline(escrow)
        if deadline is not None and deadline <= utcnow():
            escrows_to_release.append(escrow)

    released_count = 0

    from app.routes.escrow import execute_order_escrow_release

    for escrow in escrows_to_release:
        try:
            order = escrow.order
            if not order:
                continue

            # ── Delegate entirely to the unified release pipeline ─────────────
            # execute_order_escrow_release handles:
            #   • Flutterwave transfer payout (with SPLIT: guard)
            #   • Paystack transfer payout (with subaccount split guard)
            #   • Daya payout
            #   • Vendor ledger credit (idempotent)
            #   • In-app notifications (buyer + vendor)
            #   • Email notifications (buyer + vendor)
            #   • order.status = COMPLETED + escrow.status = RELEASED
            #   • db.session.commit()
            result = execute_order_escrow_release(order, escrow, source="auto_release")
            if not result:
                logger.warning(f"  [AUTO-RELEASE] execute_order_escrow_release returned False for Order #{order.id}")
                continue

            released_count += 1
            logger.info(f"  ✓ Auto-released escrow {escrow.transaction_number} for Order #{order.id}")

        except Exception as e:
            db.session.rollback()
            logger.info(f"  ✗ Error auto-releasing escrow {escrow.transaction_number}: {e}")
            continue

    logger.info(f"[{utcnow()}] Auto-release task completed. Released {released_count} escrow(s).")
    return released_count


def send_delivery_reminders():
    """
    Remind buyers, about 24 hours before the Safe Pay deadline, to confirm
    delivery or report a problem: in-app for signed-in buyers and by email for
    every buyer with a real address (guest Pay Link buyers included).
    Runs every hour; one reminder per order.
    """
    logger.info(f"[{utcnow()}] Running delivery reminder task...")

    # Find all delivered escrow transactions and filter dynamically based on release thresholds
    delivered_escrows = EscrowTransaction.query.filter(
        EscrowTransaction.status == EscrowStatus.DELIVERED
    ).all()

    escrows_to_remind = []
    for escrow in delivered_escrows:
        order = escrow.order
        if not order:
            continue

        from app.services.trust import release_deadline
        deadline = release_deadline(escrow)
        if deadline is not None and utcnow() < deadline <= utcnow() + timedelta(hours=24):
            escrows_to_remind.append((escrow, deadline))

    reminded_count = 0

    for escrow, deadline in escrows_to_remind:
        try:
            order = escrow.order
            if not order:
                continue

            # One reminder per order: the reminder time on the escrow row is the marker.
            if escrow.buyer_reminded_at is not None:
                continue
            escrow.buyer_reminded_at = utcnow()

            when = format_deadline(deadline)
            if order.buyer_id:
                db.session.add(Notification(
                    user_id=order.buyer_id,
                    title="Confirm Your Delivery",
                    message=(f"Did order #{order.id} arrive as described? Confirm it, or report a problem, "
                             f"by {when}. If you do neither, the seller is paid then."),
                    type="ORDER",
                    order_id=order.id,
                ))

            db.session.commit()
            email_buyer_confirm_by(order, deadline, reminder=True)
            reminded_count += 1

            logger.info(f"  ✓ Sent delivery reminder for Order #{order.id}")

        except Exception as e:
            db.session.rollback()
            logger.info(f"  ✗ Error sending reminder for escrow {escrow.transaction_number}: {e}")
            continue

    logger.info(f"[{utcnow()}] Delivery reminder task completed. Sent {reminded_count} reminder(s).")
    return reminded_count


# ── Unpaid orders: one reminder, then a predictable cancellation ─────────────

# Orders that are not "unpaid" in the Safe Pay sense: pay on delivery is
# collected by the seller, free claims cost nothing.
_NOT_PREPAID_METHODS = ("POD", "FREE")
# Daya payment states where the buyer's money may be on its way or has arrived
# but isn't confirmed yet. PENDING only counts while its rate hasn't expired.
_DAYA_IN_FLIGHT = ("RECEIVED", "REQUIRES_REVIEW")
UNPAID_REMINDER_AFTER = timedelta(minutes=45)
UNPAID_REMINDER_UNTIL = timedelta(hours=20)


def _aware(dt):
    if dt is None:
        return None
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt


def pending_payment_cancel_hours() -> int:
    """How long an unpaid order waits before it is cancelled:
    PENDING_PAYMENT_CANCEL_HOURS, default 24, kept between 1 and 72."""
    import os
    try:
        hours = int(os.environ.get("PENDING_PAYMENT_CANCEL_HOURS", "24"))
    except (TypeError, ValueError):
        hours = 24
    return max(1, min(72, hours))


def _unpaid_started_at(order, escrow):
    return _aware(getattr(escrow, "created_at", None)) or _aware(getattr(order, "created_at", None))


def _is_prepaid_unpaid(order, escrow) -> bool:
    """True for an order still waiting for the buyer to pay online."""
    if order is None or escrow is None or escrow.status != EscrowStatus.PENDING_PAYMENT:
        return False
    if (order.payment_method or "").upper() in _NOT_PREPAID_METHODS:
        return False
    if (order.status or "").upper() in ("CANCELLED", "PENDING_DELIVERY"):
        return False
    payable = float(order.total_amount or 0) + float(order.logistics_fee or 0)
    return payable > 0


def unpaid_cancel_at(order, escrow):
    """When an unpaid order will be cancelled, or None if it isn't waiting for payment."""
    if not _is_prepaid_unpaid(order, escrow):
        return None
    start = _unpaid_started_at(order, escrow)
    return start + timedelta(hours=pending_payment_cancel_hours()) if start else None


def daya_payment_in_flight(order_id, include_open_accounts=True) -> bool:
    """True while a Daya bank transfer / crypto payment for this order may still
    land: money received but not confirmed, under review, or (when
    include_open_accounts) a payment account whose rate hasn't expired yet.
    Such orders are not cancelled."""
    from app.models.withdrawal import DayaPayment
    now = utcnow()
    for dp in DayaPayment.query.filter_by(order_id=order_id).all():
        status = (dp.status or "").upper()
        if status in _DAYA_IN_FLIGHT:
            return True
        if status == "PENDING" and include_open_accounts:
            expires = _aware(dp.rate_expires_at)
            if expires is not None and expires > now:
                return True
    return False


def _money(order, amount) -> str:
    currency = (getattr(order, "currency", None) or "NGN").upper()
    return f"₦{amount:,.2f}" if currency == "NGN" else f"{currency} {amount:,.2f}"


def _buyer_email_for(order):
    """The buyer's real email: the address on the order, else their account's."""
    email = buyer_email_address(order)
    if email:
        return email
    buyer = getattr(order, "buyer", None)
    return real_email(buyer.email) if buyer is not None else None


def _buyer_first_name(order):
    try:
        if order.buyer is not None and order.buyer.first_name:
            return order.buyer.first_name
    except Exception:  # noqa: BLE001
        pass
    name = (getattr(order, "buyer_name", None) or "").strip()
    return name.split(" ")[0] if name and "@" not in name else "there"


def _items_summary(order) -> str:
    parts = []
    for it in order.items:
        name = it.product.name if it.product else "Item"
        parts.append(f"{name} x{it.quantity}" if (it.quantity or 1) > 1 else name)
    return ", ".join(parts) or "your items"


def _store_name(order):
    try:
        sf = order.vendor.storefront if order.vendor else None
        return sf.store_name if sf and sf.store_name else None
    except Exception:  # noqa: BLE001
        return None


def _order_again_url(order) -> str:
    for it in order.items:
        if it.product is not None and getattr(it.product, "is_active", True):
            return f"https://siiqo.com/products/{it.product.id}"
    try:
        sf = order.vendor.storefront if order.vendor else None
        if sf and sf.store_slug:
            return f"https://siiqo.com/{sf.store_slug}"
    except Exception:  # noqa: BLE001
        pass
    return "https://siiqo.com"


def send_unpaid_reminders():
    """
    One email (and an in-app notice for signed-in buyers) for an order that is
    still waiting for payment 45 minutes after it was placed, with the private
    link to finish paying and the exact time it will be cancelled.
    Runs every hour.
    """
    from app.routes.escrow import generate_order_token
    from app.utils.email import send_siiqo_email

    now = utcnow()
    hours = pending_payment_cancel_hours()
    newest = now - UNPAID_REMINDER_AFTER
    oldest = now - min(UNPAID_REMINDER_UNTIL, timedelta(hours=hours))

    rows = (EscrowTransaction.query
            .join(Order, Order.id == EscrowTransaction.order_id)
            .filter(EscrowTransaction.status == EscrowStatus.PENDING_PAYMENT,
                    EscrowTransaction.created_at <= newest,
                    EscrowTransaction.created_at >= oldest,
                    Order.payment_reminded_at.is_(None))
            .all())

    sent = 0
    for escrow in rows:
        order = escrow.order
        try:
            if not _is_prepaid_unpaid(order, escrow):
                continue
            # Money already sent and being confirmed: don't tell them it's missing.
            if daya_payment_in_flight(order.id, include_open_accounts=False):
                continue
            email = _buyer_email_for(order)
            if not email and not order.buyer_id:
                continue
            cancel_at = unpaid_cancel_at(order, escrow)
            when = format_deadline(cancel_at)
            link = f"https://siiqo.com/order-confirm/{order.id}?token={generate_order_token(order.id)}"
            total = _money(order, float(order.total_amount or 0) + float(order.logistics_fee or 0))
            store = _store_name(order)

            order.payment_reminded_at = now
            if order.buyer_id:
                db.session.add(Notification(
                    user_id=order.buyer_id,
                    title=f"Your order #{order.id} is waiting",
                    message=(f"Order #{order.id} ({total}) hasn't been paid yet. "
                             f"Pay by {when} or it will be cancelled."),
                    type="ORDER",
                    order_id=order.id,
                ))
            db.session.commit()

            if email:
                send_siiqo_email(
                    to_email=email,
                    subject=f"Your order #{order.id} is waiting",
                    template_name="base_notification",
                    first_name=_buyer_first_name(order),
                    title="Your order is waiting for payment",
                    message=(f"You ordered {_items_summary(order)}"
                             f"{' from ' + store if store else ''}. Total: {total}. "
                             f"We haven't received your payment yet. Complete your payment here: {link} "
                             f"If it isn't paid within {hours} hours of ordering, by {when}, "
                             "the order will be cancelled. Nothing has been charged."),
                    cta_text="Complete payment",
                    cta_url=link,
                )
            sent += 1
        except Exception as e:  # noqa: BLE001
            db.session.rollback()
            logger.info(f"  ✗ Unpaid reminder failed for order {getattr(order, 'id', '?')}: {e}")
    logger.info(f"[{utcnow()}] Unpaid reminders sent: {sent}")
    return sent


def check_pending_payments():
    """
    Cancel orders still unpaid PENDING_PAYMENT_CANCEL_HOURS (default 24) after
    they were placed, put the stock back and tell the buyer once. Orders with a
    Daya payment still on its way are left alone. Runs every hour.
    """
    logger.info(f"[{utcnow()}] Running pending payment check task...")

    hours = pending_payment_cancel_hours()
    cutoff_time = utcnow() - timedelta(hours=hours)

    stale_escrows = EscrowTransaction.query.filter(
        EscrowTransaction.status == EscrowStatus.PENDING_PAYMENT,
        EscrowTransaction.created_at <= cutoff_time
    ).all()

    cancelled_count = 0

    for escrow in stale_escrows:
        try:
            order = escrow.order
            if not order:
                continue
            if (order.payment_method or "").upper() in _NOT_PREPAID_METHODS:
                continue
            if daya_payment_in_flight(order.id):
                logger.info(f"  … Order #{order.id} has a Daya payment in progress; not cancelled")
                continue

            # Restore stock quantity for the items in the cancelled order
            for item in order.items:
                if item.product:
                    item.product.stock_quantity = (item.product.stock_quantity or 0) + item.quantity

            # Cancel order and escrow
            escrow.status = EscrowStatus.CANCELLED
            order.status = 'CANCELLED'

            notice = (f"Order #{order.id} was cancelled because payment didn't arrive within "
                      f"{hours} hours. Nothing was charged.")
            if order.buyer_id:
                db.session.add(Notification(
                    user_id=order.buyer_id,
                    title="Order Cancelled",
                    message=notice,
                    type="ORDER",
                    order_id=order.id,
                ))

            db.session.commit()
            cancelled_count += 1
            logger.info(f"  ✓ Cancelled stale order #{order.id}")

            # The status change to CANCELLED happens once, so this email does too.
            email = _buyer_email_for(order)
            if email:
                try:
                    from app.utils.email import send_siiqo_email
                    again = _order_again_url(order)
                    send_siiqo_email(
                        to_email=email,
                        subject=f"Order #{order.id} was cancelled",
                        template_name="base_notification",
                        first_name=_buyer_first_name(order),
                        title="Your order was cancelled",
                        message=f"{notice} You can order again: {again}",
                        cta_text="Order again",
                        cta_url=again,
                    )
                except Exception as mail_err:  # noqa: BLE001
                    logger.info(f"  [UNPAID] cancel email not sent for order {order.id}: {mail_err}")

        except Exception as e:
            db.session.rollback()
            logger.info(f"  ✗ Error cancelling escrow {escrow.transaction_number}: {e}")
            continue

    logger.info(f"[{utcnow()}] Pending payment check completed. Cancelled {cancelled_count} order(s).")
    return cancelled_count


# ── Sellers: reminders to ship paid orders ───────────────────────────────────

_SHIP_REMINDER_AT = {0: 24, 1: 48}   # reminders already sent -> hours since paid
_UNSHIPPED_ORDER_STATUSES = ("PAID", "PROCESSING")


def _needs_shipping(order) -> bool:
    """A paid physical order the seller has to send out (not pickup, not
    digital or service, not pay on delivery)."""
    from app.services.delivery import PICKUP_IDS
    if (order.payment_method or "").upper() in _NOT_PREPAID_METHODS:
        return False
    if (order.logistics_provider_id or "") in PICKUP_IDS:
        return False
    items = list(order.items)
    if not items:
        return False
    return any(((it.product.product_type if it.product else None) or "physical") == "physical"
               for it in items)


def send_ship_reminders():
    """
    Remind the seller when a paid physical order hasn't been marked shipped:
    once 24 hours after payment, again at 48 hours. Email, in-app notice, and
    Telegram when the seller has linked it. Runs every hour.
    """
    from app.utils.email import send_siiqo_email
    from app.utils.telegram import send_telegram_message

    now = utcnow()
    rows = (EscrowTransaction.query
            .join(Order, Order.id == EscrowTransaction.order_id)
            .filter(EscrowTransaction.status == EscrowStatus.IN_ESCROW,
                    Order.status.in_(_UNSHIPPED_ORDER_STATUSES),
                    db.or_(Order.ship_reminders_sent.is_(None), Order.ship_reminders_sent < 2))
            .all())

    sent = 0
    for escrow in rows:
        order = escrow.order
        try:
            paid_at = _aware(order.paid_at) or _aware(escrow.paid_at)
            if paid_at is None or not _needs_shipping(order):
                continue
            already = order.ship_reminders_sent or 0
            due_hours = _SHIP_REMINDER_AT.get(already)
            if due_hours is None or now - paid_at < timedelta(hours=due_hours):
                continue
            try:
                from app.services.disputes import payout_blocked
                if payout_blocked(order, escrow):
                    continue
            except Exception:  # noqa: BLE001
                pass

            hours_ago = int((now - paid_at).total_seconds() // 3600)
            text = (f"Order #{order.id} was paid {hours_ago} hours ago and hasn't been marked shipped. "
                    "The buyer is waiting. Ship it and mark it shipped here: https://siiqo.com/vendor/orders")
            if already >= 1:
                text += " If you can't fulfil it, cancel it so the buyer is refunded."

            order.ship_reminders_sent = already + 1
            db.session.add(Notification(
                user_id=order.vendor_id,
                title=f"Ship order #{order.id}",
                message=text,
                type="ORDER",
                order_id=order.id,
            ))
            db.session.commit()
            sent += 1

            vendor = order.vendor
            if vendor is not None and vendor.email:
                try:
                    send_siiqo_email(
                        to_email=vendor.email,
                        subject=f"Order #{order.id} is waiting to be shipped",
                        template_name="base_notification",
                        first_name=vendor.first_name or "there",
                        title="Your buyer is waiting",
                        message=text,
                        cta_text="Mark it shipped",
                        cta_url="https://siiqo.com/vendor/orders",
                    )
                except Exception as mail_err:  # noqa: BLE001
                    logger.info(f"  [SHIP] reminder email not sent for order {order.id}: {mail_err}")
            if vendor is not None and getattr(vendor, "telegram_id", None):
                try:
                    send_telegram_message(vendor.telegram_id, text.replace(
                        "https://siiqo.com/vendor/orders",
                        "<a href='https://siiqo.com/vendor/orders'>siiqo.com/vendor/orders</a>"))
                except Exception as tg_err:  # noqa: BLE001
                    logger.info(f"  [SHIP] Telegram reminder not sent for order {order.id}: {tg_err}")
        except Exception as e:  # noqa: BLE001
            db.session.rollback()
            logger.info(f"  ✗ Ship reminder failed for order {getattr(order, 'id', '?')}: {e}")
    logger.info(f"[{utcnow()}] Ship reminders sent: {sent}")
    return sent


# Main task runner (call this from cron job or scheduler)
def run_all_escrow_tasks():
    """Run all escrow background tasks"""
    logger.info("=" * 60)
    logger.info("ESCROW BACKGROUND TASKS")
    logger.info("=" * 60)

    auto_release_escrow()
    send_delivery_reminders()
    send_unpaid_reminders()
    check_pending_payments()
    send_ship_reminders()

    logger.info("=" * 60)
    logger.info("ALL TASKS COMPLETED")
    logger.info("=" * 60)


if __name__ == '__main__':
    # For testing: python -m app.tasks.escrow_tasks
    from app import create_app
    app = create_app()
    with app.app_context():
        run_all_escrow_tasks()


def format_deadline(deadline) -> str:
    """'Tue 14 Nov, 3:40 pm (Lagos time)': WAT, UTC+1."""
    wat = deadline.astimezone(timezone(timedelta(hours=1)))
    hour = wat.strftime("%I").lstrip("0") or "12"
    return f"{wat.strftime('%a')} {wat.day} {wat.strftime('%b')}, {hour}:{wat.strftime('%M')} {wat.strftime('%p').lower()} (Lagos time)"


def buyer_email_address(order):
    """The buyer's real email, or None for the guest placeholders Pay Links store."""
    return real_email(getattr(order, "buyer_email", None))


def real_email(raw):
    """raw if it is a real address, else None (empty or a guest placeholder)."""
    email = (raw or "").strip()
    if not email or "@" not in email:
        return None
    lowered = email.lower()
    if lowered.endswith("@guest.siiqo.local") or (lowered.startswith("guest-") and lowered.endswith("@siiqo.com")):
        return None
    return email


def email_buyer_confirm_by(order, deadline, reminder=False) -> None:
    """Tell the buyer the Safe Pay deadline, with their private confirm link.
    Best effort; never raises."""
    email = buyer_email_address(order)
    if not email:
        return
    try:
        from app.routes.escrow import generate_order_token, review_url
        from app.utils.email import send_siiqo_email
        link = f"https://siiqo.com/order-confirm/{order.id}?token={generate_order_token(order.id)}"
        when = format_deadline(deadline)
        store = None
        try:
            sf = order.vendor.storefront if order.vendor else None
            store = sf.store_name if sf else None
        except Exception:  # noqa: BLE001
            store = None
        seller = store or "The seller"
        send_siiqo_email(
            to_email=email,
            subject=(f"Reminder: confirm order #{order.id} by {when}" if reminder
                     else f"Order #{order.id} was marked delivered"),
            template_name="base_notification",
            first_name=((getattr(order, "buyer_name", None) or "there").split(" ")[0]),
            title=("Your Safe Pay window closes soon" if reminder else "Did your order arrive?"),
            message=(f"{seller} marked order #{order.id} as delivered. Your payment is still held by Safe Pay. "
                     f"Please confirm it arrived, or report a problem, by {when}. "
                     "If you do neither, the seller is paid then. "
                     "Keep this link private: it is only for you. Never share it with the seller."
                     + ("" if reminder else
                        f" How was it? Rate {store or 'the seller'} in one tap: {review_url(order.id)}")),
            cta_text="Confirm or report a problem",
            cta_url=link,
        )
    except Exception as exc:  # noqa: BLE001
        logger.info(f"  [SAFE PAY] confirm-by email not sent for order {getattr(order, 'id', '?')}: {exc}")
