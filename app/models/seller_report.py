"""
"Report this seller" (docs/HCD_AUDIT_2026-11.md §4): a buyer who was scammed,
or suspects a seller, can tell Siiqo from the Trust Passport or a seller check.
A person at Siiqo reviews every report; a report never changes a trust score
by itself.

All tables are new (created by db.create_all()).
"""
from datetime import datetime, timezone

from app.extensions import db


def utcnow():
    return datetime.now(timezone.utc)


class SellerReport(db.Model):
    __tablename__ = 'seller_reports'

    REASONS = ("never_delivered", "fake_or_not_as_described", "asked_to_pay_outside", "impersonation", "other")
    STATUSES = ("open", "reviewed", "dismissed")

    id = db.Column(db.Integer, primary_key=True)
    store_slug = db.Column(db.String(100), nullable=False, index=True)
    vendor_id = db.Column(db.Integer, db.ForeignKey('users.id', ondelete='SET NULL'), nullable=True, index=True)
    reason = db.Column(db.String(40), nullable=False)
    details = db.Column(db.Text, nullable=True)
    order_reference = db.Column(db.String(60), nullable=True)
    contact_email = db.Column(db.String(120), nullable=True)
    reporter_user_id = db.Column(db.Integer, db.ForeignKey('users.id', ondelete='SET NULL'), nullable=True)
    ip_hash = db.Column(db.String(64), nullable=True, index=True)
    status = db.Column(db.String(20), nullable=False, default="open", index=True)
    created_at = db.Column(db.DateTime(timezone=True), nullable=False, default=utcnow, index=True)
