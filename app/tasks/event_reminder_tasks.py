"""
event_reminder_tasks.py
Sends 24-hour reminder emails to ticket holders for upcoming events.
Called by the APScheduler job registered in app/__init__.py
"""
import logging
from datetime import datetime, timedelta

from app.extensions import db
from app.models.event import Event, TicketPurchase
from app.utils.email import send_siiqo_email

logger = logging.getLogger(__name__)


def run_event_reminders():
    """
    Sends reminder emails to all active ticket holders for events
    happening within the next 24–25 hours.
    Designed to run every hour; the 1-hour window prevents duplicate sends
    across runs while still catching any event in the next-day window.
    """
    try:
        now = datetime.utcnow()
        window_start = now + timedelta(hours=23)   # 23 h from now
        window_end   = now + timedelta(hours=25)   # 25 h from now

        # Find events starting in the next-day window
        upcoming_events = Event.query.filter(
            Event.start_date >= window_start,
            Event.start_date <= window_end,
            Event.is_published == True,
            Event.is_deleted   == False,
        ).all()

        if not upcoming_events:
            return

        sent_count = 0
        for event in upcoming_events:
            # Get all active tickets for this event
            tickets = TicketPurchase.query.filter_by(
                event_id=event.id,
                status='ACTIVE',
            ).all()

            event_date_str = event.start_date.strftime('%A, %B %d, %Y')
            event_time_str = event.start_date.strftime('%I:%M %p')
            location       = event.venue_address or event.city or ''

            for ticket in tickets:
                try:
                    send_siiqo_email(
                        to_email=ticket.buyer_email,
                        subject=f"⏰ Reminder: {event.title} is Tomorrow!",
                        template_name="event_reminder",
                        buyer_name=ticket.buyer_name or 'there',
                        event_title=event.title,
                        event_date=event_date_str,
                        event_time=event_time_str,
                        event_location=location,
                        event_format=event.event_format or 'in-person',
                        ticket_code=ticket.ticket_code,
                        tickets_url='https://siiqo.com/buyer/tickets',
                        year=now.year,
                    )
                    sent_count += 1
                except Exception as e:
                    logger.warning(f"Reminder email failed for ticket {ticket.ticket_code}: {e}")

        logger.info(f"[EVENT REMINDERS] Sent {sent_count} reminder emails for {len(upcoming_events)} upcoming events")

    except Exception as e:
        logger.error(f"[EVENT REMINDERS] Task failed: {e}")
