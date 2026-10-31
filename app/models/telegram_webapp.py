"""
Telegram Mini App sign-in: replay protection (Phase T, T-3).

Each Telegram ``initData`` string carries a ``hash``. Once a hash has been used
to sign in it is stored here and refused if it is sent again. Rows older than
the freshness window are useless (the auth_date check already refuses them),
so they are pruned on the next sign-in. New table: created by db.create_all().
"""
from datetime import datetime, timezone

from app.extensions import db


def utcnow():
    return datetime.now(timezone.utc)


class TelegramInitDataUse(db.Model):
    __tablename__ = 'telegram_initdata_uses'

    # The initData ``hash`` field (64 hex chars). Primary key, so two workers
    # racing on the same initData cannot both insert it.
    init_hash = db.Column(db.String(64), primary_key=True)
    telegram_id = db.Column(db.String(50), nullable=False, index=True)
    auth_date = db.Column(db.Integer, nullable=False)
    used_at = db.Column(db.DateTime(timezone=True), default=utcnow, nullable=False, index=True)
