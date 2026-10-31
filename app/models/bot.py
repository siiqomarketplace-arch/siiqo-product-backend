"""
Bot (Telegram / WhatsApp) internal state (Phase T, T-2 and T-4; Phase W, W-1).

All four tables are new and are created by db.create_all():

  bot_sessions         one opaque, bot-encrypted session blob per (platform, user)
                       with a TTL. The backend never decrypts it.
  bot_request_nonces   replay guard for signed bot -> backend writes.
  bot_rate_counters    fixed-window counters (per-user intent / voice limits),
                       shared by every gunicorn worker through the database.
  bot_intent_cache     AI intent results for common phrasings, keyed by a hash
                       of the normalised text (the text itself is never stored).
"""
from datetime import datetime, timezone

from app.extensions import db


def utcnow():
    return datetime.now(timezone.utc)


class BotSession(db.Model):
    __tablename__ = 'bot_sessions'
    __table_args__ = (
        db.UniqueConstraint('platform', 'user_ref', name='uq_bot_sessions_platform_user'),
    )

    id = db.Column(db.Integer, primary_key=True)
    platform = db.Column(db.String(16), nullable=False)
    user_ref = db.Column(db.String(64), nullable=False)
    # Encrypted by the bot (AES-256-GCM); stored and returned as-is.
    payload = db.Column(db.Text, nullable=False)
    size_bytes = db.Column(db.Integer, nullable=False, default=0)
    expires_at = db.Column(db.DateTime(timezone=True), nullable=False, index=True)
    created_at = db.Column(db.DateTime(timezone=True), default=utcnow, nullable=False)
    updated_at = db.Column(db.DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False)


class BotRequestNonce(db.Model):
    __tablename__ = 'bot_request_nonces'

    # Primary key, so two workers racing on a replayed request cannot both insert it.
    nonce = db.Column(db.String(64), primary_key=True)
    seen_at = db.Column(db.DateTime(timezone=True), default=utcnow, nullable=False, index=True)


class BotRateCounter(db.Model):
    __tablename__ = 'bot_rate_counters'

    # "<kind>:<window>:<platform>:<user>:<window index>"
    key = db.Column(db.String(160), primary_key=True)
    count = db.Column(db.Integer, nullable=False, default=0)
    expires_at = db.Column(db.DateTime(timezone=True), nullable=False, index=True)


class BotIntentCache(db.Model):
    __tablename__ = 'bot_intent_cache'

    # sha256 hex of the normalised text.
    text_hash = db.Column(db.String(64), primary_key=True)
    intent = db.Column(db.String(32), nullable=False)
    confidence = db.Column(db.Float, nullable=False)
    prompt_version = db.Column(db.String(16), nullable=False)
    hits = db.Column(db.Integer, nullable=False, default=0)
    created_at = db.Column(db.DateTime(timezone=True), default=utcnow, nullable=False)
    expires_at = db.Column(db.DateTime(timezone=True), nullable=False, index=True)
