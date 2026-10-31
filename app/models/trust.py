from app.extensions import db
from datetime import datetime, timezone

def utcnow():
    return datetime.now(timezone.utc)

class VendorTrustProfile(db.Model):
    __tablename__ = 'vendor_trust_profiles'

    id = db.Column(db.Integer, primary_key=True)
    vendor_id = db.Column(db.Integer, db.ForeignKey('users.id'), unique=True, nullable=False)

    # Sub-scores
    completion_score = db.Column(db.Numeric(5, 2), default=0.00, nullable=False)     # Max 400
    satisfaction_score = db.Column(db.Numeric(5, 2), default=0.00, nullable=False)   # Max 250
    responsiveness_score = db.Column(db.Numeric(5, 2), default=0.00, nullable=False) # Max 150
    compliance_score = db.Column(db.Numeric(5, 2), default=0.00, nullable=False)     # Max 150
    community_score = db.Column(db.Numeric(5, 2), default=0.00, nullable=False)      # Max 50

    # Totals
    total_trust_score = db.Column(db.Integer, default=500, nullable=False)
    trust_tier = db.Column(db.String(20), default='SILVER', nullable=False)          # Safe default for pre-existing vendors

    last_recalculated = db.Column(db.DateTime(timezone=True), default=utcnow, nullable=False)
    created_at = db.Column(db.DateTime(timezone=True), default=utcnow, nullable=False)
    updated_at = db.Column(db.DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False)

    # Relationships
    vendor = db.relationship('User', backref=db.backref('trust_profile', uselist=False, cascade="all, delete-orphan"))

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "vendor_id": self.vendor_id,
            "completion_score": float(self.completion_score),
            "satisfaction_score": float(self.satisfaction_score),
            "responsiveness_score": float(self.responsiveness_score),
            "compliance_score": float(self.compliance_score),
            "community_score": float(self.community_score),
            "total_trust_score": self.total_trust_score,
            "trust_tier": self.trust_tier,
            "last_recalculated": self.last_recalculated.isoformat() if self.last_recalculated else None,
        }


class TrustScoreHistory(db.Model):
    __tablename__ = 'trust_score_history'

    id = db.Column(db.Integer, primary_key=True)
    vendor_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False)
    score_before = db.Column(db.Integer, nullable=False)
    score_after = db.Column(db.Integer, nullable=False)
    change_reason = db.Column(db.String(255), nullable=False)
    created_at = db.Column(db.DateTime(timezone=True), default=utcnow, nullable=False)

    vendor = db.relationship('User', backref='trust_history')

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "vendor_id": self.vendor_id,
            "score_before": self.score_before,
            "score_after": self.score_after,
            "change_reason": self.change_reason,
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }


class TrustScoreShadow(db.Model):
    """Trust Score v1 computed from evidence (app/services/trust_v1.py).

    One row per seller, per score version, per UTC day: re-running the daily
    job on the same day updates that row instead of adding another. While
    TRUST_SCORE_V1_LIVE is off these rows are only read by the admin
    comparison; the public score still comes from vendor_trust_profiles.
    vendor_id is a plain integer (no foreign key), like the other evidence
    tables, so a deleted account never erases the history."""
    __tablename__ = 'trust_score_shadow'
    __table_args__ = (db.UniqueConstraint('vendor_id', 'version', 'computed_on',
                                          name='uq_trust_score_shadow_day'),)

    id = db.Column(db.Integer, primary_key=True)
    vendor_id = db.Column(db.Integer, nullable=False, index=True)
    score = db.Column(db.Integer, nullable=True)            # 0-100; NULL below the minimum history
    tier = db.Column(db.String(20), nullable=False)         # NEW, BRONZE, SILVER, GOLD, PLATINUM
    components = db.Column(db.JSON, nullable=False, default=dict)
    version = db.Column(db.String(20), nullable=False)
    computed_on = db.Column(db.Date, nullable=False, index=True)
    computed_at = db.Column(db.DateTime(timezone=True), default=utcnow, nullable=False)
    # What buyers saw from the old score at the same moment, for the comparison.
    old_score = db.Column(db.Integer, nullable=True)
    old_tier = db.Column(db.String(20), nullable=True)

    def to_dict(self, with_components=True) -> dict:
        out = {
            "vendor_id": self.vendor_id,
            "score": self.score,
            "tier": self.tier,
            "version": self.version,
            "computed_on": self.computed_on.isoformat() if self.computed_on else None,
            "computed_at": self.computed_at.isoformat() if self.computed_at else None,
            "old_score": self.old_score,
            "old_tier": self.old_tier,
        }
        if with_components:
            out["components"] = self.components or {}
        return out
