"""
Safe Pay requests (docs/SAFE_PAY_REQUEST_PLAN.md).

A buyer who wants to pay a seller who is not on Siiqo sends the seller a link
from their own WhatsApp or DM. The seller opens it, sets up in a few minutes
and gets a Pay Link for that buyer. Siiqo never messages the seller.

What the buyer checked (a phone number, @handle or website) is kept only as a
keyed hash for counting demand and matching, plus a display form: handles and
websites in clear (public business contact points), phone numbers masked.

All tables are new (created by db.create_all()).
"""
from datetime import datetime, timezone

from app.extensions import db


def utcnow():
    return datetime.now(timezone.utc)


class PayRequestStatus:
    OPEN = "open"            # waiting for the seller
    READY = "ready"          # the seller accepted; a Pay Link exists
    DECLINED = "declined"    # the seller said no
    REPORTED = "reported"    # "not my business" / abuse
    ALL = (OPEN, READY, DECLINED, REPORTED)
    # "expired" and "paid" are derived (see services/pay_requests.effective_status)


class PayRequest(db.Model):
    __tablename__ = 'pay_requests'

    id = db.Column(db.Integer, primary_key=True)
    public_id = db.Column(db.String(32), unique=True, nullable=False, index=True)
    buyer_token_hash = db.Column(db.String(64), nullable=False)
    status = db.Column(db.String(20), nullable=False, default=PayRequestStatus.OPEN, index=True)

    # What the buyer checked: phone | handle | domain | store_slug | unknown
    identifier_kind = db.Column(db.String(20), nullable=True)
    identifier_hash = db.Column(db.String(64), nullable=True, index=True)
    identifier_display = db.Column(db.String(120), nullable=True)

    item = db.Column(db.String(140), nullable=True)
    amount = db.Column(db.Numeric(12, 2), nullable=True)
    currency = db.Column(db.String(10), nullable=False, default="NGN")
    buyer_email = db.Column(db.String(120), nullable=True)
    buyer_user_id = db.Column(db.Integer, db.ForeignKey('users.id', ondelete='SET NULL'), nullable=True)
    listing_id = db.Column(db.Integer, db.ForeignKey('business_listings.id', ondelete='SET NULL'), nullable=True)

    seller_user_id = db.Column(db.Integer, db.ForeignKey('users.id', ondelete='SET NULL'), nullable=True, index=True)
    payment_link_id = db.Column(db.Integer, db.ForeignKey('payment_links.id', ondelete='SET NULL'), nullable=True)

    ip_hash = db.Column(db.String(64), nullable=True, index=True)
    nudge_count = db.Column(db.Integer, nullable=False, default=0)
    close_reason = db.Column(db.String(200), nullable=True)

    created_at = db.Column(db.DateTime(timezone=True), nullable=False, default=utcnow, index=True)
    expires_at = db.Column(db.DateTime(timezone=True), nullable=False)
    accepted_at = db.Column(db.DateTime(timezone=True), nullable=True)
    closed_at = db.Column(db.DateTime(timezone=True), nullable=True)
    nudged_at = db.Column(db.DateTime(timezone=True), nullable=True)
