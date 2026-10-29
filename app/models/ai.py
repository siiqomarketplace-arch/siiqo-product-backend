"""
AI gateway metering (Siiqo 2.0, Phase 0B).

One row per gateway call that got past the kill switch: successful calls,
refusals, provider errors and calls blocked by a cap. The daily caps in
``app/services/ai/gateway.py`` are enforced from this table, so it is the
single source of truth for AI spend.

Cost is stored as an integer number of micro-US-dollars (1e-6 USD) so that
sums are exact on every database (SQLite has no native decimal type).
"""
from datetime import datetime, timezone

from app.extensions import db


def utcnow():
    return datetime.now(timezone.utc)


class AIUsage(db.Model):
    __tablename__ = 'ai_usage'

    id = db.Column(db.Integer, primary_key=True)
    created_at = db.Column(db.DateTime(timezone=True), default=utcnow, nullable=False, index=True)

    # What was asked for
    action = db.Column(db.String(64), nullable=False, index=True)
    prompt_version = db.Column(db.String(32), nullable=True)
    model = db.Column(db.String(64), nullable=True)          # model requested
    served_model = db.Column(db.String(64), nullable=True)   # model that answered (differs after a fallback)
    # users.id of the seller, deliberately without a foreign key: metering rows
    # outlive deleted users, and rows are written on a separate connection that
    # must never wait on locks the caller holds on the users table.
    seller_id = db.Column(db.Integer, nullable=True, index=True)

    # Tokens (summed across fallback attempts when the API reports them)
    input_tokens = db.Column(db.Integer, nullable=False, default=0)
    output_tokens = db.Column(db.Integer, nullable=False, default=0)
    cache_creation_input_tokens = db.Column(db.Integer, nullable=False, default=0)
    cache_read_input_tokens = db.Column(db.Integer, nullable=False, default=0)
    estimated_cost_microusd = db.Column(db.Integer, nullable=False, default=0)

    latency_ms = db.Column(db.Integer, nullable=True)

    # ok | refused | invalid_output | truncated | provider_error | unavailable | cap_exceeded
    outcome = db.Column(db.String(32), nullable=False, index=True)
    error_type = db.Column(db.String(64), nullable=True)
    stop_reason = db.Column(db.String(32), nullable=True)
    fell_back = db.Column(db.Boolean, nullable=False, default=False)
    pii_redactions = db.Column(db.Integer, nullable=False, default=0)
    request_id = db.Column(db.String(128), nullable=True)

    @property
    def estimated_cost_usd(self) -> float:
        return (self.estimated_cost_microusd or 0) / 1_000_000

    def to_dict(self):
        return {
            'id': self.id,
            'created_at': self.created_at.isoformat() if self.created_at else None,
            'action': self.action,
            'prompt_version': self.prompt_version,
            'model': self.model,
            'served_model': self.served_model,
            'seller_id': self.seller_id,
            'input_tokens': self.input_tokens,
            'output_tokens': self.output_tokens,
            'cache_creation_input_tokens': self.cache_creation_input_tokens,
            'cache_read_input_tokens': self.cache_read_input_tokens,
            'estimated_cost_usd': self.estimated_cost_usd,
            'latency_ms': self.latency_ms,
            'outcome': self.outcome,
            'error_type': self.error_type,
            'stop_reason': self.stop_reason,
            'fell_back': self.fell_back,
            'pii_redactions': self.pii_redactions,
        }
