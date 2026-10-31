"""
AI Trust Report cache (Phase 3, item 3-2).

One row per (seller, evidence hash). The narrative on /check is generated only
when the evidence behind it changes; every other read is served from here.
``source`` is "ai" (passed the code critic) or "template" (the AI failed twice
or the critic rejected it; retried after TEMPLATE_RETRY_HOURS).

The table is new, so ``db.create_all()`` creates it; no ALTER is needed.
"""
from datetime import datetime, timezone

from app.extensions import db


def utcnow():
    return datetime.now(timezone.utc)


class TrustReportNarrative(db.Model):
    __tablename__ = "trust_report_narratives"
    __table_args__ = (
        db.UniqueConstraint("vendor_id", "evidence_hash", name="uq_trust_report_vendor_hash"),
    )

    id = db.Column(db.Integer, primary_key=True)
    vendor_id = db.Column(db.Integer, db.ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    evidence_hash = db.Column(db.String(64), nullable=False, index=True)
    prompt_version = db.Column(db.String(32), nullable=False)
    source = db.Column(db.String(16), nullable=False)          # ai | template
    text = db.Column(db.Text, nullable=False)
    model = db.Column(db.String(64), nullable=True)
    attempts = db.Column(db.Integer, nullable=False, default=0)
    created_at = db.Column(db.DateTime(timezone=True), default=utcnow, nullable=False)
    updated_at = db.Column(db.DateTime(timezone=True), default=utcnow, nullable=False)
