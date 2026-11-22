"""Growth records: where each new account and store came from, and admin
email broadcasts sent in batches (app/services/attribution.py,
app/services/activation.py, app/services/broadcasts.py).

New tables only (made by db.create_all at boot and listed in the boot-time
schema list in app/__init__.py). No personal data beyond the user id: the
source fields are utm labels and marketing codes.
"""
from datetime import datetime, timezone

from app.extensions import db


def utcnow():
    return datetime.now(timezone.utc)


class UserAttribution(db.Model):
    """First touch for every new account: one row per user, never overwritten
    once a field is set. ``channel`` is resolved when the account is made and
    again when the store is created (if it was still organic)."""
    __tablename__ = 'user_attributions'

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('users.id', ondelete='CASCADE'), unique=True, nullable=False)
    utm_source = db.Column(db.String(40), nullable=True)
    utm_medium = db.Column(db.String(40), nullable=True)
    utm_campaign = db.Column(db.String(100), nullable=True)
    ref_code = db.Column(db.String(40), nullable=True)
    code_kind = db.Column(db.String(20), nullable=True)      # marketing code kind, if a code was used
    channel = db.Column(db.String(24), nullable=False, default='organic', index=True)
    signed_up_at = db.Column(db.DateTime(timezone=True), nullable=True)
    store_created_at = db.Column(db.DateTime(timezone=True), nullable=True, index=True)
    created_at = db.Column(db.DateTime(timezone=True), default=utcnow, nullable=False)
    updated_at = db.Column(db.DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False)


class BroadcastJob(db.Model):
    """An admin email broadcast, sent in the background in batches.
    Survives a restart: the sweep picks up jobs whose lease has run out."""
    __tablename__ = 'broadcast_jobs'

    STATUSES = ('queued', 'sending', 'done', 'cancelled', 'failed')

    id = db.Column(db.Integer, primary_key=True)
    audience = db.Column(db.String(40), nullable=False)
    subject = db.Column(db.String(255), nullable=False)
    body = db.Column(db.Text, nullable=False)
    critical = db.Column(db.Boolean, nullable=False, default=False)
    api_base = db.Column(db.String(255), nullable=True)     # host the unsubscribe link points at
    status = db.Column(db.String(12), nullable=False, default='queued', index=True)
    total = db.Column(db.Integer, nullable=False, default=0)
    sent = db.Column(db.Integer, nullable=False, default=0)
    failed = db.Column(db.Integer, nullable=False, default=0)
    skipped = db.Column(db.Integer, nullable=False, default=0)
    lease_owner = db.Column(db.String(64), nullable=True)
    lease_until = db.Column(db.DateTime(timezone=True), nullable=True)
    error = db.Column(db.Text, nullable=True)
    created_by_admin_id = db.Column(db.Integer, nullable=True)
    created_at = db.Column(db.DateTime(timezone=True), default=utcnow, nullable=False, index=True)
    started_at = db.Column(db.DateTime(timezone=True), nullable=True)
    finished_at = db.Column(db.DateTime(timezone=True), nullable=True)

    def to_dict(self):
        def iso(v):
            return v.isoformat() if v else None
        return {"id": self.id, "audience": self.audience, "subject": self.subject, "status": self.status,
                "critical": bool(self.critical), "total": self.total, "sent": self.sent,
                "failed": self.failed, "skipped": self.skipped,
                "pending": max(0, (self.total or 0) - (self.sent or 0) - (self.failed or 0) - (self.skipped or 0)),
                "error": self.error, "created_at": iso(self.created_at), "started_at": iso(self.started_at),
                "finished_at": iso(self.finished_at)}


class BroadcastRecipient(db.Model):
    """One recipient of a broadcast and whether the email went."""
    __tablename__ = 'broadcast_recipients'
    __table_args__ = (
        db.UniqueConstraint('job_id', 'email', name='uq_broadcast_recipient_email'),
        db.Index('ix_broadcast_recipients_job_status', 'job_id', 'status'),
    )

    STATUSES = ('pending', 'sent', 'failed', 'skipped')

    id = db.Column(db.Integer, primary_key=True)
    job_id = db.Column(db.Integer, db.ForeignKey('broadcast_jobs.id', ondelete='CASCADE'), nullable=False)
    user_id = db.Column(db.Integer, nullable=True, index=True)   # null for a typed (custom) address
    email = db.Column(db.String(255), nullable=False)
    first_name = db.Column(db.String(80), nullable=True)
    store_name = db.Column(db.String(120), nullable=True)
    status = db.Column(db.String(10), nullable=False, default='pending')
    error = db.Column(db.String(255), nullable=True)
    attempted_at = db.Column(db.DateTime(timezone=True), nullable=True)
