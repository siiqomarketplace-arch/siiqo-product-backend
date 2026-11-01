"""
Phase 4 assistants (AI_ORCHESTRATION_OPTIONS.md section 2, rows 8a and 8b).

Store concierge (8a): a buyer chat on one seller's store. Off by default; the
seller turns it on. Conversations are kept per store per buyer session. The
buyer session is stored only as a hash, the buyer's IP only as a hash, and
no buyer name, email or phone is ever asked for or kept here.

Seller copilot (8b): a read-only assistant on the seller dashboard.

All tables are new (created by ``db.create_all()``); no existing table changes.
Nothing here holds money: a concierge offer is a draft until the seller
approves it, or until the code's floor-price rule approves it when the seller
has turned on auto-accept. The Pay Link itself is always created by code.
"""
from datetime import datetime, timezone

from app.extensions import db


def utcnow():
    return datetime.now(timezone.utc)


class ConciergeSettings(db.Model):
    __tablename__ = "concierge_settings"

    id = db.Column(db.Integer, primary_key=True)
    storefront_id = db.Column(db.Integer, db.ForeignKey("storefronts.id"), unique=True, nullable=False)
    vendor_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False, index=True)
    enabled = db.Column(db.Boolean, nullable=False, default=False)
    # Founder decision Q12: off by default. When on, an offer the floor-price
    # rule accepts becomes a Pay Link at the rule price without waiting.
    auto_accept_within_floor = db.Column(db.Boolean, nullable=False, default=False)
    created_at = db.Column(db.DateTime(timezone=True), nullable=False, default=utcnow)
    updated_at = db.Column(db.DateTime(timezone=True), nullable=False, default=utcnow, onupdate=utcnow)

    def to_dict(self):
        return {
            "enabled": bool(self.enabled),
            "auto_accept_within_floor": bool(self.auto_accept_within_floor),
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }


class ConciergeConversation(db.Model):
    __tablename__ = "concierge_conversations"
    __table_args__ = (db.UniqueConstraint("storefront_id", "session_hash", name="uq_concierge_conv_session"),)

    id = db.Column(db.Integer, primary_key=True)
    storefront_id = db.Column(db.Integer, db.ForeignKey("storefronts.id"), nullable=False, index=True)
    # sha256 of the buyer's browser session id (never the id itself).
    session_hash = db.Column(db.String(64), nullable=False)
    ip_hash = db.Column(db.String(64), nullable=True)
    created_at = db.Column(db.DateTime(timezone=True), nullable=False, default=utcnow)
    updated_at = db.Column(db.DateTime(timezone=True), nullable=False, default=utcnow, onupdate=utcnow)


class ConciergeMessage(db.Model):
    __tablename__ = "concierge_messages"

    id = db.Column(db.Integer, primary_key=True)
    conversation_id = db.Column(db.Integer, db.ForeignKey("concierge_conversations.id"),
                                nullable=False, index=True)
    storefront_id = db.Column(db.Integer, nullable=False, index=True)
    role = db.Column(db.String(16), nullable=False)          # buyer | assistant
    text = db.Column(db.Text, nullable=False)
    intent = db.Column(db.String(32), nullable=True)
    # ai | template | fallback (how the assistant reply was made)
    source = db.Column(db.String(16), nullable=True)
    # True when this buyer question reached the model: the monthly cap counts these.
    ai_counted = db.Column(db.Boolean, nullable=False, default=False)
    product_ids = db.Column(db.JSON, nullable=True)
    offer_id = db.Column(db.Integer, nullable=True)
    created_at = db.Column(db.DateTime(timezone=True), nullable=False, default=utcnow, index=True)

    def to_dict(self):
        return {
            "id": self.id,
            "role": self.role,
            "text": self.text,
            "source": self.source,
            "product_ids": self.product_ids or [],
            "offer_id": self.offer_id,
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }


class ConciergeOffer(db.Model):
    """A price the buyer asked for, checked by the code rule engine.

    status:
      pending_seller  draft waiting for the seller (the default path)
      declined        the seller said no
      link_created    code created the Pay Link (seller approval, or auto-accept)
      expired         nobody acted in time
    """
    __tablename__ = "concierge_offers"

    id = db.Column(db.Integer, primary_key=True)
    storefront_id = db.Column(db.Integer, db.ForeignKey("storefronts.id"), nullable=False, index=True)
    vendor_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False, index=True)
    conversation_id = db.Column(db.Integer, db.ForeignKey("concierge_conversations.id"), nullable=False, index=True)
    product_id = db.Column(db.Integer, db.ForeignKey("products.id"), nullable=False)
    quantity = db.Column(db.Integer, nullable=False, default=1)
    currency = db.Column(db.String(10), nullable=False, default="NGN")
    list_price = db.Column(db.Numeric(12, 2), nullable=False)       # unit price on the catalogue
    offered_price = db.Column(db.Numeric(12, 2), nullable=False)    # unit price the buyer asked for
    agreed_price = db.Column(db.Numeric(12, 2), nullable=False)     # unit price the rule allows
    rule_decision = db.Column(db.String(32), nullable=False)        # accept | needs_seller
    status = db.Column(db.String(20), nullable=False, default="pending_seller", index=True)
    auto_accepted = db.Column(db.Boolean, nullable=False, default=False)
    payment_link_id = db.Column(db.Integer, db.ForeignKey("payment_links.id"), nullable=True)
    decided_at = db.Column(db.DateTime(timezone=True), nullable=True)
    created_at = db.Column(db.DateTime(timezone=True), nullable=False, default=utcnow)

    product = db.relationship("Product")
    payment_link = db.relationship("PaymentLink")

    def total(self):
        return float(self.agreed_price) * int(self.quantity or 1)

    def to_dict(self, for_seller: bool = False):
        link = self.payment_link
        data = {
            "id": self.id,
            "product_id": self.product_id,
            "product_name": self.product.name if self.product else None,
            "quantity": self.quantity,
            "currency": self.currency,
            "list_price": str(self.list_price),
            "agreed_price": str(self.agreed_price),
            "total": f"{self.total():.2f}",
            "status": self.status,
            "auto_accepted": bool(self.auto_accepted),
            "pay_link_slug": link.slug if (link and self.status == "link_created") else None,
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }
        if for_seller:
            data["offered_price"] = str(self.offered_price)
            data["rule_decision"] = self.rule_decision
            data["decided_at"] = self.decided_at.isoformat() if self.decided_at else None
        return data


class CopilotMessage(db.Model):
    """One seller question and the copilot's answer (one row each)."""
    __tablename__ = "copilot_messages"

    id = db.Column(db.Integer, primary_key=True)
    vendor_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False, index=True)
    # A client-made id grouping the questions of one panel session.
    thread = db.Column(db.String(64), nullable=False, index=True)
    role = db.Column(db.String(16), nullable=False)          # seller | assistant
    text = db.Column(db.Text, nullable=False)
    # answered | fallback | cap | disabled | step_cap
    outcome = db.Column(db.String(16), nullable=True)
    # Names and arguments of the READ tools used (never their results).
    tool_calls = db.Column(db.JSON, nullable=True)
    actions = db.Column(db.JSON, nullable=True)
    # True when the question reached the model: the monthly cap counts these.
    ai_counted = db.Column(db.Boolean, nullable=False, default=False)
    created_at = db.Column(db.DateTime(timezone=True), nullable=False, default=utcnow, index=True)
