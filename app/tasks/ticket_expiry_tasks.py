"""
ticket_expiry_tasks.py
======================
Automatically cancels pending manual-payment ticket registrations
that have not been approved by the vendor within 48 hours.

Runs every hour via APScheduler.

Steps per expired ticket:
  1. Set status = 'CANCELLED', manual_payment_status = 'EXPIRED'
  2. Send email to buyer notifying them their slot has been released
  3. DO NOT delete the payment_proof_url (kept for audit)
"""
import logging
from datetime import datetime, timezone

logger = logging.getLogger(__name__)


def run_ticket_expiry_check():
    """
    Scans all TicketPurchase records where:
      - manual_payment_status == 'PENDING'
      - expires_at < utcnow()
    and cancels them with status = EXPIRED.
    """
    try:
        from app.extensions import db
        from app.models.event import TicketPurchase
        from app.utils.email import send_siiqo_email

        now = datetime.now(timezone.utc)

        expired_tickets = TicketPurchase.query.filter(
            TicketPurchase.manual_payment_status == 'PENDING',
            TicketPurchase.expires_at.isnot(None),
            TicketPurchase.expires_at <= now,
        ).all()

        if not expired_tickets:
            return

        cancelled_count = 0
        for ticket in expired_tickets:
            try:
                ticket.status = 'CANCELLED'
                ticket.manual_payment_status = 'EXPIRED'

                # Send expiry email to buyer (non-blocking, best-effort)
                try:
                    event = ticket.event
                    if event and ticket.buyer_email:
                        send_siiqo_email(
                            to_email=ticket.buyer_email,
                            subject=f"⏰ Registration Expired — {event.title}",
                            template_name="manual_payment_rejected_buyer",
                            buyer_name=ticket.buyer_name or "there",
                            event_title=event.title,
                            rejection_reason=(
                                "Your payment submission expired after 48 hours "
                                "without vendor confirmation. You're welcome to "
                                "register again if tickets are still available."
                            ),
                            resubmit_url=f"https://siiqo.com/marketplace/events/{event.slug}",
                            organizer_email=event.contact_email or "support@siiqo.com",
                            year=now.year,
                        )
                except Exception as email_err:
                    logger.warning(
                        f"[TICKET EXPIRY] Email failed for ticket {ticket.ticket_code}: {email_err}"
                    )

                cancelled_count += 1

            except Exception as ticket_err:
                logger.error(
                    f"[TICKET EXPIRY] Error processing ticket {ticket.id}: {ticket_err}"
                )

        if cancelled_count:
            db.session.commit()
            logger.info(
                f"[TICKET EXPIRY] Cancelled {cancelled_count} expired pending manual tickets"
            )

    except Exception as e:
        logger.error(f"[TICKET EXPIRY] Task failed: {e}")
        try:
            from app.extensions import db
            db.session.rollback()
        except Exception:
            pass
