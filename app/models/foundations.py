"""
Phase 0B data foundations (docs/SIIQO_2.0_MASTER_PLAN.md §6).

These tables are append-only records that trust and AI rely on. They use
plain integer ids instead of foreign keys, so deleting or anonymising a user
or order can never erase them. Trust evidence and funnel events reuse the
existing trust_evidence and platform_events tables (app/models/telemetry.py).
Rows are written automatically by app/services/foundations.py.
"""
from datetime import datetime, timezone

from app.extensions import db


def utcnow():
    return datetime.now(timezone.utc)


class OrderStatusHistory(db.Model):
    """Every order status change, wherever in the code it happens."""
    __tablename__ = 'order_status_history'

    id = db.Column(db.Integer, primary_key=True)
    order_id = db.Column(db.Integer, nullable=False, index=True)
    from_status = db.Column(db.String(50), nullable=True)
    to_status = db.Column(db.String(50), nullable=False)
    created_at = db.Column(db.DateTime(timezone=True), default=utcnow, nullable=False)


class FeeLedgerEntry(db.Model):
    """Siiqo's platform fee per order: CHARGED when the buyer pays, REVERSED on refund."""
    __tablename__ = 'fee_ledger'
    __table_args__ = (db.UniqueConstraint('escrow_id', 'event', name='uq_fee_ledger_escrow_event'),)

    id = db.Column(db.Integer, primary_key=True)
    order_id = db.Column(db.Integer, nullable=False, index=True)
    escrow_id = db.Column(db.Integer, nullable=False)
    vendor_id = db.Column(db.Integer, nullable=False, index=True)
    event = db.Column(db.String(20), nullable=False)          # CHARGED | REVERSED
    gross_amount = db.Column(db.Numeric(12, 2), nullable=False)
    fee_amount = db.Column(db.Numeric(12, 2), nullable=False)
    fee_percent = db.Column(db.Numeric(5, 2), nullable=True)
    currency = db.Column(db.String(10), nullable=False, default='NGN')
    created_at = db.Column(db.DateTime(timezone=True), default=utcnow, nullable=False)


class SellerIdentifier(db.Model):
    """Identifier graph: which phones, emails, handles, domains, Pay Link slugs
    and payout accounts belong to which seller. Payout accounts are stored only
    as a keyed hash."""
    __tablename__ = 'seller_identifiers'
    __table_args__ = (db.UniqueConstraint('vendor_id', 'kind', 'value', name='uq_seller_identifier'),)

    id = db.Column(db.Integer, primary_key=True)
    vendor_id = db.Column(db.Integer, nullable=False, index=True)
    kind = db.Column(db.String(30), nullable=False)
    # phone, email, store_slug, domain, handle, paylink_slug, payout_account_hash
    value = db.Column(db.String(255), nullable=False, index=True)
    source = db.Column(db.String(30), nullable=True)
    first_seen = db.Column(db.DateTime(timezone=True), default=utcnow, nullable=False)
    last_seen = db.Column(db.DateTime(timezone=True), default=utcnow, nullable=False)
