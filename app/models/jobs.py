"""
Background jobs (Siiqo 2.0, Phase 1).

One row per job. The runner in ``app/services/jobs.py`` claims a row with a
conditional UPDATE (queued -> running), so several web processes can share
the table safely. The same rows will be used when jobs move to RQ.
"""
from datetime import datetime, timezone

from app.extensions import db


def utcnow():
    return datetime.now(timezone.utc)


class Job(db.Model):
    __tablename__ = 'jobs'

    STATUSES = ('queued', 'running', 'done', 'failed')

    id = db.Column(db.Integer, primary_key=True)
    type = db.Column(db.String(64), nullable=False, index=True)
    payload = db.Column(db.JSON, nullable=False, default=dict)
    # queued | running | done | failed
    status = db.Column(db.String(16), nullable=False, default='queued', index=True)
    attempts = db.Column(db.Integer, nullable=False, default=0)
    max_attempts = db.Column(db.Integer, nullable=False, default=1)
    error = db.Column(db.Text, nullable=True)
    result = db.Column(db.JSON, nullable=True)
    # users.id of whoever the job is for (caps, ownership). No foreign key:
    # job rows are operational records and may outlive the user.
    user_id = db.Column(db.Integer, nullable=True, index=True)

    created_at = db.Column(db.DateTime(timezone=True), nullable=False, default=utcnow, index=True)
    started_at = db.Column(db.DateTime(timezone=True), nullable=True)
    finished_at = db.Column(db.DateTime(timezone=True), nullable=True)
    updated_at = db.Column(db.DateTime(timezone=True), nullable=False, default=utcnow, onupdate=utcnow)

    def to_dict(self):
        return {
            'id': self.id,
            'type': self.type,
            'status': self.status,
            'attempts': self.attempts,
            'error': self.error,
            'result': self.result,
            'created_at': self.created_at.isoformat() if self.created_at else None,
            'started_at': self.started_at.isoformat() if self.started_at else None,
            'finished_at': self.finished_at.isoformat() if self.finished_at else None,
        }
