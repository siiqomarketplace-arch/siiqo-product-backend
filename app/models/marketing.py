from app.extensions import db
from datetime import datetime, timezone


def utcnow():
    return datetime.now(timezone.utc)


class Coupon(db.Model):
    __tablename__ = 'coupons'

    id = db.Column(db.Integer, primary_key=True)
    vendor_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False)

    code = db.Column(db.String(50), unique=True, nullable=False, index=True)
    discount_type = db.Column(db.String(20), default='PERCENTAGE')
    # PERCENTAGE, FIXED_AMOUNT
    discount_value = db.Column(db.Numeric(10, 2), nullable=False)

    usage_limit = db.Column(db.Integer, nullable=True)   # None = unlimited
    times_used = db.Column(db.Integer, default=0)

    valid_from = db.Column(db.DateTime(timezone=True), default=utcnow)
    valid_until = db.Column(db.DateTime(timezone=True), nullable=True)

    is_active = db.Column(db.Boolean, default=True)
    created_at = db.Column(db.DateTime(timezone=True), default=utcnow)

    # Relationships
    vendor = db.relationship('User')


class Campaign(db.Model):
    __tablename__ = 'campaigns'

    id = db.Column(db.Integer, primary_key=True)
    vendor_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False)

    name = db.Column(db.String(100), nullable=False)
    target_segment = db.Column(db.String(50), nullable=True)
    # ALL, VIP, AT_RISK, NEW

    status = db.Column(db.String(50), default='DRAFT')
    # DRAFT, SCHEDULED, SENT, CANCELLED
    scheduled_date = db.Column(db.DateTime(timezone=True), nullable=True)

    subject = db.Column(db.String(255), nullable=True)
    body = db.Column(db.Text, nullable=True)

    sent_count = db.Column(db.Integer, default=0)
    created_at = db.Column(db.DateTime(timezone=True), default=utcnow)

    # Relationships
    vendor = db.relationship('User')
