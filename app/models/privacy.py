"""
Data-protection records: consent choices and privacy (data-subject) requests.

These are the mechanisms behind the rights the Nigeria Data Protection Act 2023
gives people over their data (access, rectification, erasure, restriction,
objection, portability, withdrawal of consent). Having them does not by itself
make the platform compliant; the processes around them matter too.

Both tables are additive and created by db.create_all at boot.
"""
from datetime import datetime, timedelta, timezone

from app.extensions import db


def utcnow():
    return datetime.now(timezone.utc)


# ── Consent ────────────────────────────────────────────────────────────────

class ConsentPurpose:
    MARKETING_EMAIL = "marketing_email"
    MARKETING_WHATSAPP = "marketing_whatsapp"
    ANALYTICS = "analytics"
    VERIFICATION_CHECK = "verification_check"

    ALL = (MARKETING_EMAIL, MARKETING_WHATSAPP, ANALYTICS, VERIFICATION_CHECK)


# Version of the notice text the person saw when choosing. Bump a value when
# the wording for that purpose changes, so old choices can be told apart.
CONSENT_VERSIONS = {
    ConsentPurpose.MARKETING_EMAIL: "2026-10",
    ConsentPurpose.MARKETING_WHATSAPP: "2026-10",
    ConsentPurpose.ANALYTICS: "2026-10",
    ConsentPurpose.VERIFICATION_CHECK: "2026-10",
}

CONSENT_DESCRIPTIONS = {
    ConsentPurpose.MARKETING_EMAIL: "Product news and offers from Siiqo by email.",
    ConsentPurpose.MARKETING_WHATSAPP: "Product news and offers from Siiqo on WhatsApp.",
    ConsentPurpose.ANALYTICS: "Using how you use Siiqo to improve the product.",
    ConsentPurpose.VERIFICATION_CHECK: "Checking your identity or business with a verification provider.",
}


class Consent(db.Model):
    """Append-only log of consent choices. The latest row per (user, purpose)
    is the current choice; older rows are the history and are never edited."""
    __tablename__ = "consents"

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    purpose = db.Column(db.String(40), nullable=False)
    version = db.Column(db.String(20), nullable=False)
    granted = db.Column(db.Boolean, nullable=False)
    # Where the choice came from: user (privacy settings), unsubscribe_link, admin, signup
    source = db.Column(db.String(30), nullable=False, default="user")
    created_at = db.Column(db.DateTime(timezone=True), default=utcnow, nullable=False, index=True)

    __table_args__ = (
        db.Index("idx_consent_user_purpose", "user_id", "purpose", "id"),
    )

    def to_dict(self) -> dict:
        return {
            "purpose": self.purpose,
            "version": self.version,
            "granted": bool(self.granted),
            "source": self.source,
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }


# ── Privacy requests ───────────────────────────────────────────────────────

class PrivacyRequestType:
    ACCESS = "access"
    CORRECTION = "correction"
    DELETION = "deletion"
    RESTRICTION = "restriction"
    OBJECTION = "objection"
    PORTABILITY = "portability"

    ALL = (ACCESS, CORRECTION, DELETION, RESTRICTION, OBJECTION, PORTABILITY)


class PrivacyRequestStatus:
    OPEN = "open"
    DONE = "done"
    REJECTED = "rejected"

    ALL = (OPEN, DONE, REJECTED)


# Internal target for answering a request. Not a statement of the legal deadline.
PRIVACY_REQUEST_TARGET_DAYS = 30


class PrivacyRequest(db.Model):
    """A request from a person about their own data. Self-service exports are
    recorded here too (channel='self_service', already done)."""
    __tablename__ = "privacy_requests"

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    type = db.Column(db.String(20), nullable=False, index=True)
    details = db.Column(db.Text, nullable=True)
    status = db.Column(db.String(20), nullable=False, default=PrivacyRequestStatus.OPEN, index=True)
    channel = db.Column(db.String(20), nullable=False, default="request")  # request | self_service
    resolution_note = db.Column(db.Text, nullable=True)  # shown to the person
    resolved_by_admin_id = db.Column(db.Integer, db.ForeignKey("admin_users.id"), nullable=True)
    created_at = db.Column(db.DateTime(timezone=True), default=utcnow, nullable=False, index=True)
    due_at = db.Column(db.DateTime(timezone=True), nullable=True)
    resolved_at = db.Column(db.DateTime(timezone=True), nullable=True)

    @staticmethod
    def default_due(created=None):
        return (created or utcnow()) + timedelta(days=PRIVACY_REQUEST_TARGET_DAYS)

    def to_dict(self, admin: bool = False) -> dict:
        data = {
            "id": self.id,
            "type": self.type,
            "details": self.details,
            "status": self.status,
            "channel": self.channel,
            "resolution_note": self.resolution_note,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "due_at": self.due_at.isoformat() if self.due_at else None,
            "resolved_at": self.resolved_at.isoformat() if self.resolved_at else None,
        }
        if admin:
            data["user_id"] = self.user_id
            data["resolved_by_admin_id"] = self.resolved_by_admin_id
        return data
