"""
Mandatory verification (D11, VERIFICATION_REQUIRED_FOR): what is held until
a seller verifies. New table, created by db.create_all().
"""
from datetime import datetime, timezone

from app.extensions import db


def utcnow():
    return datetime.now(timezone.utc)


class VerificationHold(db.Model):
    """Something waiting for the seller to verify.

    kind 'payout': one order's payout. The money stays the seller's: a ledger
    DEBIT with `reference` keeps it out of the withdrawable balance while
    held, and release writes the matching CREDIT, then sends the payout.
    kind 'store': a new store created unpublished; release publishes it.
    Nothing is ever cancelled or forfeited by this table."""
    __tablename__ = 'verification_holds'

    id = db.Column(db.Integer, primary_key=True)
    vendor_id = db.Column(db.Integer, nullable=False, index=True)
    kind = db.Column(db.String(10), nullable=False)            # payout | store
    order_id = db.Column(db.Integer, nullable=True, index=True)
    storefront_id = db.Column(db.Integer, nullable=True)
    amount_ngn = db.Column(db.Numeric(14, 2), nullable=True)
    reference = db.Column(db.String(120), nullable=True, unique=True)
    mode = db.Column(db.String(40), nullable=False)
    # held | released
    status = db.Column(db.String(16), nullable=False, default='held', index=True)
    # payout release: dispatched (sent by the gateway) | balance (left withdrawable)
    release_result = db.Column(db.String(20), nullable=True)
    created_at = db.Column(db.DateTime(timezone=True), default=utcnow)
    released_at = db.Column(db.DateTime(timezone=True), nullable=True)
