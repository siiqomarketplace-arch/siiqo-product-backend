"""
"""
withdrawal.py - Vendor Withdrawal Models
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
    
    # Paystack recipient code (for manual transfers)
    recipient_code = db.Column(db.String(100), nullable=True, unique=True)

    # Paystack subaccount code (for split payments at checkout)
    paystack_subaccount_code = db.Column(db.String(100), nullable=True)
    
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
            'paystack_subaccount_code': self.paystack_subaccount_code,
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


class PartnerBankAccount(db.Model):
    """Store logistics partner bank account details for payouts"""
    __tablename__ = 'partner_bank_accounts'

    id = db.Column(db.Integer, primary_key=True)
    partner_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False, index=True)
    
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
    partner = db.relationship('User', backref='partner_bank_accounts')
    
    def to_dict(self):
        return {
            'id': self.id,
            'partner_id': self.partner_id,
            'bank_name': self.bank_name,
            'bank_code': self.bank_code,
            'account_number': self.account_number,
            'account_name': self.account_name,
            'is_verified': self.is_verified,
            'is_default': self.is_default,
            'verified_at': self.verified_at.isoformat() if self.verified_at else None,
            'created_at': self.created_at.isoformat() if self.created_at else None,
        }


class PartnerWithdrawal(db.Model):
    """Track partner withdrawal requests and payouts"""
    __tablename__ = 'partner_withdrawals'

    id = db.Column(db.Integer, primary_key=True)
    partner_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False, index=True)
    bank_account_id = db.Column(db.Integer, db.ForeignKey('partner_bank_accounts.id'), nullable=False)
    
    # Withdrawal details
    withdrawal_number = db.Column(
        db.String(100), unique=True, nullable=False, index=True,
        default=lambda: f"PWD-{uuid.uuid4().hex[:10].upper()}"
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
    partner = db.relationship('User', foreign_keys=[partner_id], backref='partner_withdrawals')
    bank_account = db.relationship('PartnerBankAccount', backref='partner_withdrawals')
    
    def to_dict(self):
        return {
            'id': self.id,
            'partner_id': self.partner_id,
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


# =============================================================================
# VendorCryptoWallet — Daya crypto payout / payment acceptance settings
# One row per vendor. Created on first POST /vendor/crypto-wallet.
# =============================================================================

class VendorCryptoWallet(db.Model):
    """
    Stores a vendor's crypto wallet for:
    1. Accepting buyer payments in USDT/USDC via Daya
    2. Receiving crypto payouts instead of NGN bank transfer

    Validation of address format is done on the frontend before saving.
    The backend stores it as-is after a basic regex check.
    """
    __tablename__ = 'vendor_crypto_wallets'

    id            = db.Column(db.Integer, primary_key=True)
    vendor_id     = db.Column(db.Integer, db.ForeignKey('users.id'),
                              nullable=False, unique=True, index=True)

    # Wallet address — TRC20 (34 chars starting T) or EVM (0x + 40 hex)
    wallet_address  = db.Column(db.String(100), nullable=False)

    # USDT or USDC
    asset           = db.Column(db.String(10), nullable=False, default='USDT')

    # TRC20 | ERC20 | BASE | BEP20
    network         = db.Column(db.String(20), nullable=False, default='TRC20')

    # When True the crypto payment option is shown to buyers at checkout
    accepts_crypto  = db.Column(db.Boolean, nullable=False, default=False)

    # Daya customer_id — created once via POST /v1/customers, reused for all
    # subsequent funding-account calls for this vendor's buyers
    daya_customer_id = db.Column(db.String(100), nullable=True)

    created_at = db.Column(db.DateTime(timezone=True), default=utcnow)
    updated_at = db.Column(db.DateTime(timezone=True), default=utcnow,
                           onupdate=utcnow)

    vendor = db.relationship('User', backref=db.backref('crypto_wallet',
                                                        uselist=False))

    def to_dict(self) -> dict:
        return {
            'vendor_id':      self.vendor_id,
            'wallet_address': self.wallet_address,
            'asset':          self.asset,
            'network':        self.network,
            'accepts_crypto': self.accepts_crypto,
        }


# =============================================================================
# DayaPayment — tracks one Daya funding-account / deposit lifecycle per order
# =============================================================================

class DayaPayment(db.Model):
    """
    Created when a buyer chooses "Pay with Crypto" at checkout.
    Tracks the Daya funding account and deposit until COMPLETED or FAILED.
    """
    __tablename__ = 'daya_payments'

    id                  = db.Column(db.Integer, primary_key=True)
    order_id            = db.Column(db.Integer, db.ForeignKey('orders.id'),
                                    nullable=False, index=True)
    # The buyer (stored for Daya customer creation if needed)
    buyer_id            = db.Column(db.Integer, db.ForeignKey('users.id'),
                                    nullable=False)

    # "ngn_onramp" — buyer transfers NGN, Daya converts to stablecoin
    # "crypto_direct" — buyer sends USDT/USDC directly
    payment_type        = db.Column(db.String(20), nullable=False)

    # Daya-returned funding account details
    daya_funding_account_id = db.Column(db.String(100), nullable=True)
    daya_rate_id        = db.Column(db.String(100), nullable=True)
    rate_expires_at     = db.Column(db.DateTime(timezone=True), nullable=True)

    # NGN amount shown to buyer (for onramp) or NGN equivalent (for direct)
    amount_ngn          = db.Column(db.Numeric(12, 2), nullable=False)
    # Crypto amount to send (for direct; NULL for onramp until rate fetched)
    amount_crypto       = db.Column(db.String(30), nullable=True)  # e.g. "10.52"
    asset               = db.Column(db.String(10), nullable=True)  # USDT | USDC
    network             = db.Column(db.String(20), nullable=True)  # TRC20 etc.

    # Daya payment instructions shown to buyer
    bank_name           = db.Column(db.String(100), nullable=True)  # onramp
    account_number      = db.Column(db.String(30),  nullable=True)  # onramp
    account_name        = db.Column(db.String(150), nullable=True)  # onramp
    wallet_address      = db.Column(db.String(100), nullable=True)  # direct

    # Deposit status mirrored from Daya
    # PENDING | RECEIVED | PROCESSING | COMPLETED | FAILED | EXPIRED
    status              = db.Column(db.String(20), nullable=False,
                                    default='PENDING', index=True)

    # Daya deposit id once deposit.received fires
    daya_deposit_id     = db.Column(db.String(100), nullable=True)

    # FX rate at time of initiation (NGN per 1 stablecoin) — for display only
    rate                = db.Column(db.Numeric(12, 4), nullable=True)

    created_at = db.Column(db.DateTime(timezone=True), default=utcnow)
    updated_at = db.Column(db.DateTime(timezone=True), default=utcnow,
                           onupdate=utcnow)

    order = db.relationship('Order',
                            backref=db.backref('daya_payment', uselist=False))
    buyer = db.relationship('User', foreign_keys=[buyer_id])

    def to_dict(self) -> dict:
        return {
            'id':                       self.id,
            'order_id':                 self.order_id,
            'payment_type':             self.payment_type,
            'daya_funding_account_id':  self.daya_funding_account_id,
            'amount_ngn':               str(self.amount_ngn),
            'amount_crypto':            self.amount_crypto,
            'asset':                    self.asset,
            'network':                  self.network,
            'bank_name':                self.bank_name,
            'account_number':           self.account_number,
            'account_name':             self.account_name,
            'wallet_address':           self.wallet_address,
            'status':                   self.status,
            'rate_expires_at':          self.rate_expires_at.isoformat() if self.rate_expires_at else None,
            'created_at':               self.created_at.isoformat() if self.created_at else None,
        }
