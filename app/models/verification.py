"""
Verification v2 — provider-based identity and business verification.

Additive tables only (created by db.create_all at boot); no existing table is
altered. The NIN / RC number is never stored: only a keyed hash for duplicate
detection. Documents and selfies stay with the provider.
"""
from datetime import datetime, timezone

from app.extensions import db


def utcnow():
    return datetime.now(timezone.utc)


class VerificationKind:
    IDENTITY = "IDENTITY"
    BUSINESS = "BUSINESS"


class CaseStatus:
    AWAITING_PAYMENT = "AWAITING_PAYMENT"
    IN_PROGRESS = "IN_PROGRESS"            # seller is in the provider's hosted step
    PENDING_PROVIDER = "PENDING_PROVIDER"  # waiting for the provider's result
    PASSED = "PASSED"
    FAILED_RETRYABLE = "FAILED_RETRYABLE"  # capture problem: seller may retry
    FAILED = "FAILED"                      # final: data mismatch
    FAILED_FRAUD = "FAILED_FRAUD"          # final: spoof / fraud signal
    ON_HOLD = "ON_HOLD"                    # needs support (e.g. duplicate identity, name mismatch)
    EXPIRED = "EXPIRED"                    # seller never finished the hosted step

    FINAL = {PASSED, FAILED, FAILED_FRAUD, ON_HOLD, EXPIRED}


class VerificationCase(db.Model):
    """One verification attempt (identity or business) for one seller."""
    __tablename__ = "verification_cases"

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False, index=True)
    kind = db.Column(db.String(20), nullable=False)
    seller_type = db.Column(db.String(20), nullable=False, default="INDIVIDUAL")  # INDIVIDUAL | BUSINESS
    provider = db.Column(db.String(30), nullable=False)
    provider_ref = db.Column(db.String(120), nullable=True, index=True)
    status = db.Column(db.String(30), nullable=False, default=CaseStatus.IN_PROGRESS, index=True)
    reason_code = db.Column(db.String(60), nullable=True)
    idempotency_key = db.Column(db.String(80), nullable=False, unique=True)

    # Keyed hash of the NIN (identity) or RC/BN number (business). Never the raw value.
    id_number_hash = db.Column(db.String(64), nullable=True, index=True)

    # Legal names returned by the provider, used for name matching and display opt-in.
    verified_name = db.Column(db.String(255), nullable=True)
    business_name = db.Column(db.String(255), nullable=True)

    consent_version = db.Column(db.String(20), nullable=True)
    consent_at = db.Column(db.DateTime(timezone=True), nullable=True)
    payment_reference = db.Column(db.String(120), nullable=True)
    cost_kobo = db.Column(db.Integer, nullable=True)

    hosted_url = db.Column(db.String(500), nullable=True)
    created_at = db.Column(db.DateTime(timezone=True), default=utcnow, nullable=False)
    completed_at = db.Column(db.DateTime(timezone=True), nullable=True)

    checks = db.relationship("VerificationCheck", backref="case", lazy="dynamic",
                             cascade="all, delete-orphan")

    def to_seller_dict(self) -> dict:
        """What the seller may see about their own case (no hashes, no provider data)."""
        return {
            "id": self.id,
            "kind": self.kind,
            "seller_type": self.seller_type,
            "status": self.status,
            "reason_code": self.reason_code,
            "hosted_url": self.hosted_url if self.status == CaseStatus.IN_PROGRESS else None,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "completed_at": self.completed_at.isoformat() if self.completed_at else None,
        }


class VerificationCheck(db.Model):
    """A sub-check inside a case (nin_lookup, liveness, face_match, cac_lookup, ...)."""
    __tablename__ = "verification_checks"

    id = db.Column(db.Integer, primary_key=True)
    case_id = db.Column(db.Integer, db.ForeignKey("verification_cases.id"), nullable=False, index=True)
    check_type = db.Column(db.String(40), nullable=False)
    result = db.Column(db.String(20), nullable=False)  # PASS | FAIL | INCONCLUSIVE
    score = db.Column(db.Float, nullable=True)
    threshold = db.Column(db.Float, nullable=True)
    detail_code = db.Column(db.String(60), nullable=True)
    created_at = db.Column(db.DateTime(timezone=True), default=utcnow, nullable=False)


class VerificationStatus(db.Model):
    """Current, derived verification state per seller (one row per user)."""
    __tablename__ = "verification_status"

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False, unique=True)
    seller_type = db.Column(db.String(20), nullable=True)
    identity_verified_at = db.Column(db.DateTime(timezone=True), nullable=True)
    business_verified_at = db.Column(db.DateTime(timezone=True), nullable=True)
    valid_until = db.Column(db.DateTime(timezone=True), nullable=True)
    subscription_active = db.Column(db.Boolean, default=False, nullable=False)
    # Provider that produced the current identity result. "mock" results are
    # for staff testing only and are never shown to buyers.
    source_provider = db.Column(db.String(30), nullable=True)
    updated_at = db.Column(db.DateTime(timezone=True), default=utcnow, onupdate=utcnow)

    def _aware(self, dt):
        if dt is not None and dt.tzinfo is None:
            return dt.replace(tzinfo=timezone.utc)
        return dt

    def is_identity_current(self, now=None) -> bool:
        now = now or utcnow()
        valid_until = self._aware(self.valid_until)
        return bool(self.identity_verified_at and valid_until and valid_until > now)

    def public_flags(self, now=None) -> dict:
        """The only verification facts buyers ever see (never numbers or documents)."""
        identity = (
            self.is_identity_current(now)
            and self.subscription_active
            and self.source_provider not in (None, "mock")
        )
        return {
            "identity_verified": identity,
            "business_registered": bool(identity and self.business_verified_at),
        }
