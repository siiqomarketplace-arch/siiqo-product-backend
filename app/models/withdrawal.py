"""
withdrawal.py — Vendor Withdrawal Models
Handles vendor bank accounts and withdrawal requests
"""
from app.extensions import db
from datetime import datetime, timezone
import uuid


def utcnow():
    return datetime.now(timezone.utc)


class VendorBankAccount(db.Model):
    """Store vendor bank account details for payouts"""
    __tablename__ = 'vendor_bank_accounts'

    id = db.Column(db.Integer, primary_key=True)
    vendor_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False, index=True)
    
    # Bank details
    bank_name = db.Column(db.String(100), nullable=False)
    bank_code = db.Column(db.String(10), nullable=False)  # Paystack bank code
    account_number = db.Column(db.String(20), nullable=False)
    account_name = db.Column(db.String(255), nullable=False)
    
    # Paystack recipient code (for transfers)
    recipient_code = db.Column(db.String(100), nullable=True, unique=True)
    
    # Verification
    is_verified = db.Column(db.Boolean, default=False)
    verified_at = db.Column(db.DateTime(timezone=True), nullable=True)
    
    # Default account
    is_default = db.Column(db.Boolean, default=False)
    
    # Timestamps
    created_at = db.Column(db.DateTime(timezone=True), default=utcnow)
    updated_at = db.Column(db.DateTime(timezone=True), default=utcnow, onupdate=utcnow)
    
    # Relationships
    vendor = db.relationship('User', backref='bank_accounts')
    
    def to_dict(self):
        return {
            'id': self.id,
            'vendor_id': self.vendor_id,
            'bank_name': self.bank_name,
            'bank_code': self.bank_code,
            'account_number': self.account_number,
            'account_name': self.account_name,
            'is_verified': self.is_verified,
            'is_default': self.is_default,
            'verified_at': self.verified_at.isoformat() if self.verified_at else None,
            'created_at': self.created_at.isoformat() if self.created_at else None,
        }


class Withdrawal(db.Model):
    """Track vendor withdrawal requests and payouts"""
    __tablename__ = 'withdrawals'

    id = db.Column(db.Integer, primary_key=True)
    vendor_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False, index=True)
    bank_account_id = db.Column(db.Integer, db.ForeignKey('vendor_bank_accounts.id'), nullable=False)
    
    # Withdrawal details
    withdrawal_number = db.Column(
        db.String(100), unique=True, nullable=False, index=True,
        default=lambda: f"WD-{uuid.uuid4().hex[:10].upper()}"
    )
    amount = db.Column(db.Numeric(10, 2), nullable=False)
    currency = db.Column(db.String(10), default='NGN')
    
    # Fees
    fee_amount = db.Column(db.Numeric(10, 2), default=50.00)  # Paystack transfer fee
    net_amount = db.Column(db.Numeric(10, 2), nullable=False)  # Amount - fee
    
    # Status tracking
    status = db.Column(db.String(50), default='PENDING', index=True)
    # PENDING, PROCESSING, COMPLETED, FAILED, CANCELLED
    
    # Paystack transfer details
    transfer_code = db.Column(db.String(100), nullable=True)
    transfer_reference = db.Column(db.String(100), nullable=True)
    
    # Failure tracking
    failure_reason = db.Column(db.Text, nullable=True)
    retry_count = db.Column(db.Integer, default=0)
    
    # Admin actions
    approved_by = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=True)
    approved_at = db.Column(db.DateTime(timezone=True), nullable=True)
    rejected_by = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=True)
    rejected_at = db.Column(db.DateTime(timezone=True), nullable=True)
    rejection_reason = db.Column(db.Text, nullable=True)
    
    # Timestamps
    requested_at = db.Column(db.DateTime(timezone=True), default=utcnow)
    processed_at = db.Column(db.DateTime(timezone=True), nullable=True)
    completed_at = db.Column(db.DateTime(timezone=True), nullable=True)
    
    # Relationships
    vendor = db.relationship('User', foreign_keys=[vendor_id], backref='withdrawals')
    bank_account = db.relationship('VendorBankAccount', backref='withdrawals')
    approver = db.relationship('User', foreign_keys=[approved_by])
    rejecter = db.relationship('User', foreign_keys=[rejected_by])
    
    def to_dict(self):
        return {
            'id': self.id,
            'vendor_id': self.vendor_id,
            'withdrawal_number': self.withdrawal_number,
            'amount': str(self.amount),
            'fee_amount': str(self.fee_amount),
            'net_amount': str(self.net_amount),
            'currency': self.currency,
            'status': self.status,
            'bank_account': self.bank_account.to_dict() if self.bank_account else None,
            'transfer_code': self.transfer_code,
            'failure_reason': self.failure_reason,
            'requested_at': self.requested_at.isoformat() if self.requested_at else None,
            'completed_at': self.completed_at.isoformat() if self.completed_at else None,
        }


class PODPayment(db.Model):
    """Track Pay on Delivery cash payments"""
    __tablename__ = 'pod_payments'

    id = db.Column(db.Integer, primary_key=True)
    order_id = db.Column(db.Integer, db.ForeignKey('orders.id'), unique=True, nullable=False, index=True)
    vendor_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False, index=True)
    
    # Payment details
    amount = db.Column(db.Numeric(10, 2), nullable=False)
    currency = db.Column(db.String(10), default='NGN')
    
    # Confirmation
    confirmed_by_vendor = db.Column(db.Boolean, default=False)
    confirmed_at = db.Column(db.DateTime(timezone=True), nullable=True)
    
    # Payment method (cash, card, transfer at pickup)
    payment_method = db.Column(db.String(50), default='CASH')
    # CASH, CARD, TRANSFER
    
    # Notes
    vendor_notes = db.Column(db.Text, nullable=True)
    
    # Reconciliation
    reconciled = db.Column(db.Boolean, default=False)
    reconciled_at = db.Column(db.DateTime(timezone=True), nullable=True)
    reconciled_by = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=True)
    
    # Timestamps
    created_at = db.Column(db.DateTime(timezone=True), default=utcnow)
    updated_at = db.Column(db.DateTime(timezone=True), default=utcnow, onupdate=utcnow)
    
    # Relationships
    order = db.relationship('Order', backref='pod_payment')
    vendor = db.relationship('User', foreign_keys=[vendor_id], backref='pod_payments')
    reconciler = db.relationship('User', foreign_keys=[reconciled_by])
    
    def to_dict(self):
        return {
            'id': self.id,
            'order_id': self.order_id,
            'vendor_id': self.vendor_id,
            'amount': str(self.amount),
            'currency': self.currency,
            'confirmed_by_vendor': self.confirmed_by_vendor,
            'confirmed_at': self.confirmed_at.isoformat() if self.confirmed_at else None,
            'payment_method': self.payment_method,
            'vendor_notes': self.vendor_notes,
            'reconciled': self.reconciled,
            'reconciled_at': self.reconciled_at.isoformat() if self.reconciled_at else None,
            'created_at': self.created_at.isoformat() if self.created_at else None,
        }
