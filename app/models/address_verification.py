"""
Address verification (V5, Phase 4). Optional for sellers, off by default
(ADDRESS_VERIFICATION_ENABLED).

New table, created by db.create_all(); no existing table is altered.

Only the outcome, its dates and the provider's reference are stored. The
address itself and the NIN used to create the provider's candidate are sent
to the provider and never saved by Siiqo.
"""
from datetime import datetime, timezone

from app.extensions import db


def utcnow():
    return datetime.now(timezone.utc)


class AddressStatus:
    PENDING = "PENDING"                    # sent; the provider's agent has not reported yet
    VERIFIED = "VERIFIED"
    NOT_VERIFIED = "NOT_VERIFIED"
    UNABLE_TO_VERIFY = "UNABLE_TO_VERIFY"  # agent could not complete the visit
    CANCELLED = "CANCELLED"

    FINAL = {VERIFIED, NOT_VERIFIED, UNABLE_TO_VERIFY, CANCELLED}


class AddressVerification(db.Model):
    """One address check for one seller."""
    __tablename__ = "address_verifications"

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False, index=True)
    provider = db.Column(db.String(30), nullable=False)
    # The provider's id for this check (Youverify: the address request `id`).
    provider_ref = db.Column(db.String(120), nullable=True, index=True)
    status = db.Column(db.String(20), nullable=False, default=AddressStatus.PENDING, index=True)
    reason_code = db.Column(db.String(60), nullable=True)

    consent_version = db.Column(db.String(20), nullable=True)
    consent_at = db.Column(db.DateTime(timezone=True), nullable=True)

    requested_at = db.Column(db.DateTime(timezone=True), default=utcnow, nullable=False)
    last_checked_at = db.Column(db.DateTime(timezone=True), nullable=True)
    # When the outcome arrived (the "date" of the claim).
    completed_at = db.Column(db.DateTime(timezone=True), nullable=True)
