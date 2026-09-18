from app.extensions import db
from datetime import datetime, timezone

def utcnow():
    return datetime.now(timezone.utc)


class PlatformEvent(db.Model):
    """
    Siiqo 2.0 Observability Engine.
    Append-only stream of user, buyer, and business actions.
    """
    __tablename__ = 'platform_events'

    id = db.Column(db.BigInteger().with_variant(db.Integer, "sqlite"), primary_key=True)
    event_name = db.Column(db.String(100), nullable=False, index=True)
    timestamp = db.Column(db.DateTime(timezone=True), default=utcnow, index=True, nullable=False)
    
    session_id = db.Column(db.String(100), nullable=True, index=True)
    user_id = db.Column(db.Integer, db.ForeignKey('users.id', ondelete='SET NULL'), nullable=True)
    business_id = db.Column(db.Integer, db.ForeignKey('users.id', ondelete='SET NULL'), nullable=True, index=True)
    storefront_id = db.Column(db.Integer, db.ForeignKey('storefronts.id', ondelete='SET NULL'), nullable=True)
    product_id = db.Column(db.Integer, db.ForeignKey('products.id', ondelete='SET NULL'), nullable=True)
    order_id = db.Column(db.Integer, db.ForeignKey('orders.id', ondelete='SET NULL'), nullable=True)
    
    source = db.Column(db.String(50), nullable=True)  # 'homepage', 'marketplace', 'storefront', 'payment_link'
    properties = db.Column(db.JSON, nullable=True, default=dict)  # Query context, filters, counts, etc.

    # Relationships
    user = db.relationship('User', foreign_keys=[user_id])
    business = db.relationship('User', foreign_keys=[business_id])
    storefront = db.relationship('Storefront', foreign_keys=[storefront_id])
    product = db.relationship('Product', foreign_keys=[product_id])
    order = db.relationship('Order', foreign_keys=[order_id])

    def to_dict(self):
        return {
            "id": self.id,
            "event_name": self.event_name,
            "timestamp": self.timestamp.isoformat() if self.timestamp else None,
            "session_id": self.session_id,
            "user_id": self.user_id,
            "business_id": self.business_id,
            "storefront_id": self.storefront_id,
            "product_id": self.product_id,
            "order_id": self.order_id,
            "source": self.source,
            "metadata": self.properties or {},
        }


class TrustEvidence(db.Model):
    """
    Siiqo 2.0 Verifiable Commercial Trust Ledger.
    Fact-based evidence repository behind vendor trust badges.
    """
    __tablename__ = 'trust_evidence'

    id = db.Column(db.BigInteger().with_variant(db.Integer, "sqlite"), primary_key=True)
    business_id = db.Column(db.Integer, db.ForeignKey('users.id', ondelete='CASCADE'), nullable=False, index=True)
    
    # Evidence Classification
    evidence_type = db.Column(db.String(100), nullable=False, index=True)
    # e.g. 'identity_verified', 'phone_verified', 'bank_verified', 'order_fulfilled',
    #      'escrow_released', 'review_received', 'repeat_purchase'
    
    provenance = db.Column(db.String(50), nullable=False, default='business_claimed')
    # 'business_claimed', 'business_verified', 'siiqo_transaction_verified', 'publicly_observed'
    
    source = db.Column(db.String(100), nullable=False)
    # e.g. 'orders', 'escrow_transactions', 'reviews', 'kyc'
    
    source_id = db.Column(db.String(100), nullable=True)  # Reference ID in the source system
    status = db.Column(db.String(50), default='ACTIVE', nullable=False)  # 'ACTIVE', 'REVOKED', 'SUPERSEDED'
    confidence = db.Column(db.Numeric(3, 2), default=1.00, nullable=False)
    
    properties = db.Column(db.JSON, nullable=True, default=dict)
    created_at = db.Column(db.DateTime(timezone=True), default=utcnow, index=True, nullable=False)
    verified_at = db.Column(db.DateTime(timezone=True), nullable=True)

    business = db.relationship('User', foreign_keys=[business_id], backref='trust_evidence_records')

    def to_dict(self):
        return {
            "id": self.id,
            "business_id": self.business_id,
            "evidence_type": self.evidence_type,
            "provenance": self.provenance,
            "source": self.source,
            "source_id": self.source_id,
            "status": self.status,
            "confidence": float(self.confidence) if self.confidence is not None else 1.0,
            "properties": self.properties or {},
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "verified_at": self.verified_at.isoformat() if self.verified_at else None,
        }
