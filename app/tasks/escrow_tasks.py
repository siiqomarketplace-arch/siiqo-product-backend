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


def check_pending_payments():
    """
    Check for orders stuck in PENDING_PAYMENT for more than 24 hours and cancel them.
    Run this task daily.
    """
    logger.info(f"[{utcnow()}] Running pending payment check task...")

    # Find escrow transactions that are PENDING_PAYMENT for more than 1 hour
    cutoff_time = utcnow() - timedelta(hours=1)

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

            # Restore stock quantity for the items in the cancelled order
            for item in order.items:
                if item.product:
                    item.product.stock_quantity = (item.product.stock_quantity or 0) + item.quantity

            # Cancel order and escrow
            escrow.status = EscrowStatus.CANCELLED
            order.status = 'CANCELLED'

            # Notify buyer
            if order.buyer_id:
                db.session.add(Notification(
                    user_id=order.buyer_id,
                    title="Order Cancelled",
                    message=f"Order #{order.id} was cancelled due to non-payment within 1 hour.",
                    type="ORDER",
                    order_id=order.id,
                ))

            db.session.commit()
            cancelled_count += 1

            logger.info(f"  ✓ Cancelled stale order #{order.id}")

        except Exception as e:
            db.session.rollback()
            logger.info(f"  ✗ Error cancelling escrow {escrow.transaction_number}: {e}")
            continue

    logger.info(f"[{utcnow()}] Pending payment check completed. Cancelled {cancelled_count} order(s).")
    return cancelled_count


# Main task runner (call this from cron job or scheduler)
def run_all_escrow_tasks():
    """Run all escrow background tasks"""
    logger.info("=" * 60)
    logger.info("ESCROW BACKGROUND TASKS")
    logger.info("=" * 60)

    auto_release_escrow()
    send_delivery_reminders()
    check_pending_payments()

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
    email = (getattr(order, "buyer_email", None) or "").strip()
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
        from app.routes.escrow import generate_order_token
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
                     "Keep this link private: it is only for you. Never share it with the seller."),
            cta_text="Confirm or report a problem",
            cta_url=link,
        )
    except Exception as exc:  # noqa: BLE001
        logger.info(f"  [SAFE PAY] confirm-by email not sent for order {getattr(order, 'id', '?')}: {exc}")
