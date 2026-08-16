"""
fee_sweep.py — SiiqoFeeSweep model

Tracks every platform fee auto-sweep transfer from Siiqo's Daya
collection balance to Siiqo's corporate NGN bank account.
"""

from app.extensions import db
from datetime import datetime, timezone


def _utcnow():
    return datetime.now(timezone.utc)


class SiiqoFeeSweep(db.Model):
    __tablename__ = "siiqo_fee_sweeps"

    id = db.Column(db.Integer, primary_key=True)

    # Unique reference sent to Daya as the idempotency key
    reference = db.Column(db.String(100), unique=True, nullable=False, index=True)

    # Amounts
    amount_ngn = db.Column(db.Numeric(12, 2), nullable=False)   # NGN swept
    amount_usd = db.Column(db.Numeric(10, 4), nullable=True)    # USD equivalent at time of sweep
    fx_rate    = db.Column(db.Numeric(10, 2), nullable=True)    # NGN/USD rate used

    # Destination bank (Siiqo corporate)
    bank_code      = db.Column(db.String(20), nullable=False)
    account_number = db.Column(db.String(20), nullable=False)
    account_name   = db.Column(db.String(255), nullable=True)

    # Daya response
    daya_transfer_id = db.Column(db.String(255), nullable=True)

    # Status: PENDING | SUCCESS | FAILED
    status        = db.Column(db.String(20), nullable=False, default="PENDING")
    error_message = db.Column(db.Text, nullable=True)

    # Timestamps
    created_at   = db.Column(db.DateTime(timezone=True), default=_utcnow, nullable=False)
    completed_at = db.Column(db.DateTime(timezone=True), nullable=True)

    def to_dict(self) -> dict:
        return {
            "id":               self.id,
            "reference":        self.reference,
            "amount_ngn":       str(self.amount_ngn),
            "amount_usd":       str(self.amount_usd) if self.amount_usd else None,
            "fx_rate":          str(self.fx_rate) if self.fx_rate else None,
            "bank_code":        self.bank_code,
            "account_number":   self.account_number,
            "account_name":     self.account_name,
            "daya_transfer_id": self.daya_transfer_id,
            "status":           self.status,
            "error_message":    self.error_message,
            "created_at":       self.created_at.isoformat() if self.created_at else None,
            "completed_at":     self.completed_at.isoformat() if self.completed_at else None,
        }
