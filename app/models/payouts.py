"""Payout records that make a seller's payout impossible to lose.

PayoutTransfer: every bank transfer Siiqo sends (an order payout, a seller
withdrawal, a delivery partner withdrawal) with its gateway, reference, amount
and FINAL status. A transfer the gateway accepted is "pending" until its
webhook or the scheduled check says success / failed / reversed
(app/services/payout_tracking.py).

ManualPayout: a payout a person sent by hand from a gateway dashboard and an
admin recorded for one order (app/services/split_settlement.py). It marks the
order's payout as settled without changing the seller's balance.

Tables are made by db.create_all() at boot (app/__init__.py).
"""
from datetime import datetime, timezone

from app.extensions import db


def utcnow():
    return datetime.now(timezone.utc)


class PayoutTransfer(db.Model):
    __tablename__ = 'payout_transfers'

    id = db.Column(db.Integer, primary_key=True)
    # 'order' (payout at release), 'withdrawal' (seller), 'partner_withdrawal'
    kind = db.Column(db.String(30), nullable=False, index=True)
    order_id = db.Column(db.Integer, nullable=True, index=True)
    withdrawal_id = db.Column(db.Integer, nullable=True, index=True)
    partner_withdrawal_id = db.Column(db.Integer, nullable=True, index=True)
    # Seller or partner whose balance gets the money back if it fails.
    user_id = db.Column(db.Integer, nullable=False, index=True)
    gateway = db.Column(db.String(20), nullable=False)          # paystack, flutterwave, daya
    reference = db.Column(db.String(120), nullable=False, unique=True)
    gateway_transfer_id = db.Column(db.String(120), nullable=True, index=True)
    amount = db.Column(db.Numeric(12, 2), nullable=False)
    # pending -> success | failed | reversed (success -> reversed is possible)
    status = db.Column(db.String(20), nullable=False, default='pending', index=True)
    gateway_status = db.Column(db.String(40), nullable=True)
    # The ledger DEBIT that took this money out of the balance (order payouts:
    # PAYOUT-<escrow ref>; withdrawals: the withdrawal reference).
    debit_reference = db.Column(db.String(120), nullable=True)
    failure_reason = db.Column(db.String(500), nullable=True)
    restored_at = db.Column(db.DateTime(timezone=True), nullable=True)
    finalized_at = db.Column(db.DateTime(timezone=True), nullable=True)
    last_checked_at = db.Column(db.DateTime(timezone=True), nullable=True)
    check_count = db.Column(db.Integer, nullable=False, default=0)
    created_at = db.Column(db.DateTime(timezone=True), default=utcnow, index=True)
    updated_at = db.Column(db.DateTime(timezone=True), default=utcnow, onupdate=utcnow)

    def to_dict(self):
        return {
            'id': self.id,
            'kind': self.kind,
            'order_id': self.order_id,
            'withdrawal_id': self.withdrawal_id,
            'partner_withdrawal_id': self.partner_withdrawal_id,
            'user_id': self.user_id,
            'gateway': self.gateway,
            'reference': self.reference,
            'amount': str(self.amount) if self.amount is not None else None,
            'status': self.status,
            'gateway_status': self.gateway_status,
            'failure_reason': self.failure_reason,
            'restored': self.restored_at is not None,
            'created_at': self.created_at.isoformat() if self.created_at else None,
            'finalized_at': self.finalized_at.isoformat() if self.finalized_at else None,
        }


class ManualPayout(db.Model):
    __tablename__ = 'manual_payouts'

    id = db.Column(db.Integer, primary_key=True)
    order_id = db.Column(db.Integer, nullable=False, unique=True)
    vendor_id = db.Column(db.Integer, nullable=False, index=True)
    amount = db.Column(db.Numeric(12, 2), nullable=False)
    gateway = db.Column(db.String(30), nullable=False)
    reference = db.Column(db.String(120), nullable=False)
    paid_on = db.Column(db.Date, nullable=False)
    note = db.Column(db.String(500), nullable=True)
    recorded_by_admin_id = db.Column(db.Integer, nullable=True)
    created_at = db.Column(db.DateTime(timezone=True), default=utcnow)

    def to_dict(self):
        return {
            'order_id': self.order_id,
            'vendor_id': self.vendor_id,
            'amount': str(self.amount),
            'gateway': self.gateway,
            'reference': self.reference,
            'paid_on': self.paid_on.isoformat() if self.paid_on else None,
            'note': self.note,
            'created_at': self.created_at.isoformat() if self.created_at else None,
        }
