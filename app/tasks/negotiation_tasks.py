"""
negotiation_tasks.py — Background tasks for negotiation expiry
Run alongside escrow_tasks via cron (e.g. every hour).
"""
import logging
logger = logging.getLogger(__name__)
from datetime import datetime, timezone

from app.extensions import db
from app.models.negotiation import NegotiationRequest, NegotiationHistory
from app.models.order import CartItem
from app.models.communication import Notification


def _utcnow():
    return datetime.now(timezone.utc)


def _make_aware(dt):
    if dt is None:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def expire_pending_negotiations():
    """
    1. Expire PENDING/COUNTERED negotiations whose 48-h response window has passed.
    2. Expire ACCEPTED negotiations whose 48-h checkout window has passed
       (reset negotiated_price on the cart item so listed price is used).
    Run every hour.
    """
    now = _utcnow()
    expired_count = 0

    # ── 1. Response-window expiry ────────────────────────────────────────
    stale = NegotiationRequest.query.filter(
        NegotiationRequest.status.in_(['PENDING', 'COUNTERED']),
        NegotiationRequest.expires_at.isnot(None),
        NegotiationRequest.expires_at <= now,
    ).all()

    for neg in stale:
        try:
            neg.status = 'EXPIRED'
            db.session.add(NegotiationHistory(
                negotiation_id=neg.id,
                actor_id=neg.vendor_id,   # system action attributed to vendor side
                action='EXPIRED',
                price=neg.current_offer,
                message='Offer expired automatically after 48 hours.',
            ))
            # Notify both parties
            db.session.add(Notification(
                user_id=neg.buyer_id,
                title="Offer Expired",
                message=f"Your offer for {neg.product.name if neg.product else 'an item'} "
                        f"expired without a response. You can make a new offer.",
                type='NEGOTIATION',
            ))
            db.session.add(Notification(
                user_id=neg.vendor_id,
                title="Offer Expired",
                message=f"An offer from a buyer for {neg.product.name if neg.product else 'an item'} "
                        f"has expired.",
                type='NEGOTIATION',
            ))
            db.session.commit()
            expired_count += 1
            logger.info(f"  ✓ Expired negotiation #{neg.id}")
        except Exception as e:
            db.session.rollback()
            logger.info(f"  ✗ Error expiring negotiation #{neg.id}: {e}")

    # ── 2. Accepted-offer checkout-window expiry ─────────────────────────
    accepted_stale = NegotiationRequest.query.filter(
        NegotiationRequest.status == 'ACCEPTED',
        NegotiationRequest.accepted_expires_at.isnot(None),
        NegotiationRequest.accepted_expires_at <= now,
    ).all()

    for neg in accepted_stale:
        try:
            neg.status = 'EXPIRED'
            # Clear negotiated price from cart item
            if neg.cart_item_id:
                ci = db.session.get(CartItem, neg.cart_item_id)
                if ci:
                    ci.negotiated_price = None
                    ci.negotiation_id = None
            db.session.add(Notification(
                user_id=neg.buyer_id,
                title="Agreed Price Expired",
                message=f"Your agreed price of ₦{float(neg.final_price):,.0f} for "
                        f"{neg.product.name if neg.product else 'an item'} has expired. "
                        f"The listed price now applies.",
                type='NEGOTIATION',
            ))
            db.session.commit()
            expired_count += 1
            logger.info(f"  ✓ Expired accepted negotiation #{neg.id} (checkout window)")
        except Exception as e:
            db.session.rollback()
            logger.info(f"  ✗ Error expiring accepted negotiation #{neg.id}: {e}")

    logger.info(f"[{now}] Negotiation expiry task done. Expired {expired_count} negotiation(s).")
    return expired_count


def send_accepted_offer_reminders():
    """
    Send a reminder to buyers who have an accepted offer expiring within 24 h.
    Run daily.
    """
    from datetime import timedelta
    now = _utcnow()
    window_end = now + timedelta(hours=24)
    reminded = 0

    soon_expiring = NegotiationRequest.query.filter(
        NegotiationRequest.status == 'ACCEPTED',
        NegotiationRequest.accepted_expires_at.isnot(None),
        NegotiationRequest.accepted_expires_at > now,
        NegotiationRequest.accepted_expires_at <= window_end,
    ).all()

    for neg in soon_expiring:
        try:
            # Avoid duplicate reminders
            already = Notification.query.filter(
                Notification.user_id == neg.buyer_id,
                Notification.title == "Agreed Price Expiring Soon",
                Notification.created_at >= now - timedelta(hours=20),
            ).first()
            if already:
                continue

            db.session.add(Notification(
                user_id=neg.buyer_id,
                title="Agreed Price Expiring Soon",
                message=f"Your agreed price of ₦{float(neg.final_price):,.0f} for "
                        f"{neg.product.name if neg.product else 'an item'} expires in less than 24 hours. "
                        f"Complete your purchase now.",
                type='NEGOTIATION',
            ))
            db.session.commit()
            reminded += 1
        except Exception as e:
            db.session.rollback()
            logger.info(f"  ✗ Reminder error for negotiation #{neg.id}: {e}")

    logger.info(f"[{now}] Sent {reminded} accepted-offer reminder(s).")
    return reminded


def run_all_negotiation_tasks():
    expire_pending_negotiations()
    send_accepted_offer_reminders()


if __name__ == '__main__':
    from app import create_app
    app = create_app()
    with app.app_context():
        run_all_negotiation_tasks()
