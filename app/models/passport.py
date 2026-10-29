from datetime import datetime, timezone

from app.extensions import db


def utcnow():
    return datetime.now(timezone.utc)


class PassportSettings(db.Model):
    """What a seller shares on their free Trust Passport. Every seller with a
    store has a Passport; it is public unless they turn it off."""
    __tablename__ = 'passport_settings'

    id = db.Column(db.Integer, primary_key=True)
    vendor_id = db.Column(db.Integer, db.ForeignKey('users.id'), unique=True, nullable=False)
    is_public = db.Column(db.Boolean, default=True, nullable=False)
    show_completed_orders = db.Column(db.Boolean, default=True, nullable=False)
    show_member_since = db.Column(db.Boolean, default=True, nullable=False)
    claimed_at = db.Column(db.DateTime(timezone=True), nullable=True)
    updated_at = db.Column(db.DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class PassportApiClient(db.Model):
    """A platform (marketplace, plugin, AI assistant) reading Passports.
    Only a hash of the key is stored; the key is shown once at creation."""
    __tablename__ = 'passport_api_clients'

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(120), nullable=False)
    key_prefix = db.Column(db.String(12), nullable=False)
    key_hash = db.Column(db.String(64), unique=True, nullable=False, index=True)
    plan = db.Column(db.String(20), default='free', nullable=False)   # free | paid (platform tiers later)
    is_active = db.Column(db.Boolean, default=True, nullable=False)
    created_at = db.Column(db.DateTime(timezone=True), default=utcnow)
    last_used_at = db.Column(db.DateTime(timezone=True), nullable=True)
