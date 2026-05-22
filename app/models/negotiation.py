"""
negotiation.py — Price negotiation models
"""
from datetime import datetime, timezone, timedelta
from app.extensions import db


def utcnow():
    return datetime.now(timezone.utc)


class NegotiationRequest(db.Model):
    __tablename__ = 'negotiation_requests'

    id              = db.Column(db.Integer, primary_key=True)
    buyer_id        = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False)
    vendor_id       = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False)
    product_id      = db.Column(db.Integer, db.ForeignKey('products.id'), nullable=False)
    cart_item_id    = db.Column(db.Integer, db.ForeignKey('cart_items.id'), nullable=True)

    # Pricing
    original_price  = db.Column(db.Numeric(10, 2), nullable=False)
    current_offer   = db.Column(db.Numeric(10, 2), nullable=False)   # latest proposed price
    final_price     = db.Column(db.Numeric(10, 2), nullable=True)    # set on ACCEPTED

    # Status: PENDING | COUNTERED | ACCEPTED | REJECTED | EXPIRED
    status          = db.Column(db.String(20), nullable=False, default='PENDING')

    # Who has the ball? 'buyer' or 'vendor'
    awaiting_reply_from = db.Column(db.String(10), nullable=False, default='vendor')

    # Optional messages
    buyer_message   = db.Column(db.Text, nullable=True)
    vendor_message  = db.Column(db.Text, nullable=True)

    quantity        = db.Column(db.Integer, default=1)

    # Timestamps
    created_at      = db.Column(db.DateTime(timezone=True), default=utcnow)
    updated_at      = db.Column(db.DateTime(timezone=True), default=utcnow, onupdate=utcnow)
    # Offer expires 48 h after last update
    expires_at      = db.Column(db.DateTime(timezone=True), nullable=True)
    # Once accepted, buyer has 48 h to checkout at agreed price
    accepted_expires_at = db.Column(db.DateTime(timezone=True), nullable=True)

    # Relationships
    buyer       = db.relationship('User', foreign_keys=[buyer_id])
    vendor      = db.relationship('User', foreign_keys=[vendor_id])
    product     = db.relationship('Product')
    cart_item   = db.relationship('CartItem', foreign_keys=[cart_item_id])
    history     = db.relationship('NegotiationHistory', back_populates='negotiation',
                                  cascade='all, delete-orphan', order_by='NegotiationHistory.created_at')

    def set_expiry(self, hours: int = 48):
        """Reset the 48-hour response window."""
        self.expires_at = utcnow() + timedelta(hours=hours)

    def is_expired(self) -> bool:
        if not self.expires_at:
            return False
        return utcnow() > self.expires_at.replace(tzinfo=timezone.utc) if self.expires_at.tzinfo is None else utcnow() > self.expires_at

    def to_dict(self) -> dict:
        product = self.product
        return {
            "id":                   self.id,
            "buyer_id":             self.buyer_id,
            "vendor_id":            self.vendor_id,
            "product_id":           self.product_id,
            "cart_item_id":         self.cart_item_id,
            "product_name":         product.name if product else "",
            "product_image":        (product.images[0] if product and product.images else None),
            "original_price":       str(self.original_price),
            "current_offer":        str(self.current_offer),
            "final_price":          str(self.final_price) if self.final_price else None,
            "status":               self.status,
            "awaiting_reply_from":  self.awaiting_reply_from,
            "buyer_message":        self.buyer_message,
            "vendor_message":       self.vendor_message,
            "quantity":             self.quantity,
            "created_at":           self.created_at.isoformat() if self.created_at else None,
            "updated_at":           self.updated_at.isoformat() if self.updated_at else None,
            "expires_at":           self.expires_at.isoformat() if self.expires_at else None,
            "accepted_expires_at":  self.accepted_expires_at.isoformat() if self.accepted_expires_at else None,
            "history":              [h.to_dict() for h in self.history],
            "buyer_name":           self.buyer.full_name if self.buyer else "",
            "vendor_name":          (self.vendor.storefront.store_name if self.vendor and self.vendor.storefront else self.vendor.full_name) if self.vendor else "",
        }


class NegotiationHistory(db.Model):
    __tablename__ = 'negotiation_history'

    id              = db.Column(db.Integer, primary_key=True)
    negotiation_id  = db.Column(db.Integer, db.ForeignKey('negotiation_requests.id'), nullable=False)
    actor_id        = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False)
    # PROPOSED | COUNTERED | ACCEPTED | REJECTED | EXPIRED
    action          = db.Column(db.String(20), nullable=False)
    price           = db.Column(db.Numeric(10, 2), nullable=False)
    message         = db.Column(db.Text, nullable=True)
    created_at      = db.Column(db.DateTime(timezone=True), default=utcnow)

    negotiation     = db.relationship('NegotiationRequest', back_populates='history')
    actor           = db.relationship('User', foreign_keys=[actor_id])

    def to_dict(self) -> dict:
        return {
            "id":               self.id,
            "negotiation_id":   self.negotiation_id,
            "actor_id":         self.actor_id,
            "actor_name":       self.actor.full_name if self.actor else "",
            "action":           self.action,
            "price":            str(self.price),
            "message":          self.message,
            "created_at":       self.created_at.isoformat() if self.created_at else None,
        }
