from datetime import datetime, timezone

from app.extensions import db


def utcnow():
    return datetime.now(timezone.utc)


class SubscriptionPayment(db.Model):
    """One row per successful Verified Pro payment (first charge or renewal).
    The unique reference makes webhook retries and duplicate events harmless."""
    __tablename__ = 'subscription_payments'

    id = db.Column(db.Integer, primary_key=True)
    vendor_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False, index=True)
    subscription_id = db.Column(db.Integer, db.ForeignKey('vendor_subscriptions.id'), nullable=True)
    reference = db.Column(db.String(120), nullable=True, unique=True)
    amount_ngn = db.Column(db.Numeric(12, 2), nullable=True)
    status = db.Column(db.String(20), nullable=False, default='PAID')  # PAID, REFUNDED, REFUND_FAILED
    refund_reason = db.Column(db.String(50), nullable=True)
    refunded_at = db.Column(db.DateTime(timezone=True), nullable=True)
    created_at = db.Column(db.DateTime(timezone=True), default=utcnow)
