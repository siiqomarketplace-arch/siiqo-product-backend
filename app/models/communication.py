from app.extensions import db
from datetime import datetime, timezone


def utcnow():
    return datetime.now(timezone.utc)


class Notification(db.Model):
    __tablename__ = 'notifications'

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False)

    title = db.Column(db.String(255), nullable=False)
    message = db.Column(db.Text, nullable=False)
    type = db.Column(db.String(50), default='SYSTEM')
    # ORDER, ESCROW, CHAT, SYSTEM, REVIEW, VENDOR_APPROVED

    is_read = db.Column(db.Boolean, default=False)
    order_id = db.Column(db.Integer, db.ForeignKey('orders.id'), nullable=True)

    created_at = db.Column(db.DateTime(timezone=True), default=utcnow)

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "title": self.title,
            "message": self.message,
            "type": self.type,
            "is_read": self.is_read,
            "order_id": self.order_id,
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }


class Message(db.Model):
    __tablename__ = 'messages'

    id = db.Column(db.Integer, primary_key=True)
    sender_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False)
    receiver_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=True)
    order_id = db.Column(db.Integer, db.ForeignKey('orders.id'), nullable=True)

    content = db.Column(db.Text, nullable=False)
    image_url = db.Column(db.String(255), nullable=True)
    is_read = db.Column(db.Boolean, default=False)

    created_at = db.Column(db.DateTime(timezone=True), default=utcnow)

    # Relationships
    sender = db.relationship('User', foreign_keys=[sender_id])
    receiver = db.relationship('User', foreign_keys=[receiver_id])


# ---------------------------------------------------------------------------
# Telegram Event Listener Hook
# ---------------------------------------------------------------------------

from sqlalchemy import event
import requests
import os
import threading

def _send_telegram_notification_async(telegram_id, title, message, type_, order_id):
    bot_url = os.environ.get('BOT_ORCHESTRATOR_URL', 'https://bot.siiqo.app')
    if not bot_url:
        return
    try:
        requests.post(f"{bot_url.rstrip('/')}/telegram-notification", json={
            "telegram_id": telegram_id,
            "title": title,
            "message": message,
            "type": type_,
            "order_id": order_id
        }, timeout=5)
    except Exception as e:
        print(f"[TG Hook Network Exception] {e}")

@event.listens_for(Notification, 'after_insert')
def after_notification_insert(mapper, connection, target):
    from app.models.user import User
    try:
        # Use connection/session-agnostic get
        user = db.session.get(User, target.user_id)
        if user and user.telegram_id:
            # Map notification type to user notification preferences
            pref_key = None
            t = str(target.type or '').upper()
            if t == 'CHAT':
                pref_key = 'chats'
            elif t in ('ORDER', 'REVIEW'):
                pref_key = 'orders'
            elif t == 'ESCROW':
                pref_key = 'payouts'

            if pref_key:
                prefs = user.telegram_notification_prefs or {}
                # If specifically toggled off, do not send
                if prefs.get(pref_key) == False:
                    return

            # Fire-and-forget in a separate thread so Flask response is unblocked
            thread = threading.Thread(
                target=_send_telegram_notification_async,
                args=(user.telegram_id, target.title, target.message, target.type, target.order_id)
            )
            thread.daemon = True
            thread.start()
    except Exception as e:
        print(f"[TG Hook Exception] {e}")

