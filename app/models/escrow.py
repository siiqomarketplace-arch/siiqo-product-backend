from app.extensions import db
from datetime import datetime, timezone
import uuid
import random


def utcnow():
    return datetime.now(timezone.utc)


class EscrowStatus:
    PENDING_PAYMENT = 'PENDING_PAYMENT'
    IN_ESCROW = 'IN_ESCROW'
    SHIPPED = 'SHIPPED'
    DELIVERED = 'DELIVERED'
    RELEASED = 'RELEASED'
    DISPUTED = 'DISPUTED'
    REFUNDED = 'REFUNDED'
    CANCELLED = 'CANCELLED'


class EscrowTransaction(db.Model):
    __tablename__ = 'escrow_transactions'

    id = db.Column(db.Integer, primary_key=True)
    order_id = db.Column(db.Integer, db.ForeignKey('orders.id'), unique=True, nullable=False)

    # Auto-generated unique reference
    transaction_number = db.Column(
        db.String(100), unique=True, nullable=False, index=True,
        default=lambda: f"ESC-{uuid.uuid4().hex[:12].upper()}"
    )
    status = db.Column(db.String(50), default=EscrowStatus.PENDING_PAYMENT, nullable=False)

    amount = db.Column(db.Numeric(10, 2), nullable=False)
    fee_percent = db.Column(db.Numeric(5, 2), default=12.00)   # Siiqo's cut
    fee_amount = db.Column(db.Numeric(10, 2), nullable=True)   # Computed at creation
    currency = db.Column(db.String(10), default='NGN')

    # PayScrow integration
    payscrow_ref = db.Column(db.String(255), nullable=True)
    payscrow_transaction_id = db.Column(db.String(100), nullable=True)  # The GUID needed for applycode
    escrow_code = db.Column(
        db.String(50), nullable=True,
        default=lambda: str(random.randint(100000, 999999))
    )               # The 6-digit release code
    payment_link = db.Column(db.String(500), nullable=True)

    dispute_id = db.Column(db.String(100), nullable=True)
    dispute_reason = db.Column(db.Text, nullable=True)

    paid_at = db.Column(db.DateTime(timezone=True), nullable=True)
    released_at = db.Column(db.DateTime(timezone=True), nullable=True)

    created_at = db.Column(db.DateTime(timezone=True), default=utcnow)
    updated_at = db.Column(db.DateTime(timezone=True), default=utcnow, onupdate=utcnow)

    # Relationships
    order = db.relationship('Order', back_populates='escrow')

    def to_dict(self) -> dict:
        # Check if this order contains event tickets
        is_event = False
        event_tickets_data = []
        try:
            from app.models.event import TicketPurchase
            tickets = TicketPurchase.query.filter_by(order_id=self.order_id).all() if self.order_id else []
            if tickets:
                is_event = True
                event_tickets_data = [
                    {
                        "ticket_code": t.ticket_code,
                        "event_title": t.event.title if t.event else "Event Ticket",
                        "ticket_type_name": t.ticket_type.name if t.ticket_type else "Ticket",
                        "status": t.status,
                        "qr_code_url": t.qr_code_url,
                        "pdf_ticket_url": t.pdf_ticket_url,
                        "venue": (t.event.venue_address or t.event.city or "") if t.event else "",
                        "start_date": t.event.start_date.isoformat() if (t.event and t.event.start_date) else None,
                    }
                    for t in tickets
                ]
        except Exception:
            pass

        is_digital_or_service = is_event or (
            all(
                (item.product.product_type if item.product else 'physical') in ('digital', 'service')
                for item in self.order.items
            ) if (self.order and self.order.items) else False
        )

        return {
            "id": self.id,
            "order_id": self.order_id,
            "transaction_number": self.transaction_number,
            "status": self.status,
            "amount": str(self.amount),
            "fee_percent": str(self.fee_percent),
            "fee_amount": str(self.fee_amount) if self.fee_amount else None,
            "currency": self.currency,
            "payment_link": self.payment_link,
            # escrow_code is the buyer's delivery OTP — needed for the buyer UI
            "escrow_code": self.escrow_code,
            "dispute_id": self.dispute_id,
            "dispute_reason": self.dispute_reason,
            "paid_at": self.paid_at.isoformat() if self.paid_at else None,
            "released_at": self.released_at.isoformat() if self.released_at else None,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "is_event": is_event,
            "event_tickets": event_tickets_data,
            "is_digital_or_service": is_digital_or_service,
            "digital_downloads": [
                {
                    "product_name": (item.product.name if item.product else "Digital Item"),
                    "file_url": (item.product.file_url if item.product else None),
                }
                for item in (self.order.items if self.order else [])
                if (item.product and item.product.product_type == 'digital' and item.product.file_url)
            ] if (self.status == EscrowStatus.RELEASED or (self.order and self.order.status == 'COMPLETED')) else [],
            "confirmation_url": (
                f"https://siiqo.com/order-confirm/{self.order_id}?token="
                f"{__import__('app.routes.escrow', fromlist=['generate_order_token']).generate_order_token(self.order_id)}"
            ) if self.order_id else None,
            "buyer_phone": (self.order.buyer_phone or getattr(self.order, 'delivery_phone', None)) if self.order else None,
            "buyer_email": self.order.buyer_email if self.order else None,
            "is_guest": bool(self.order.is_guest) if self.order else True,
        }


class LogisticsAssignment(db.Model):
    __tablename__ = 'logistics_assignments'

    id = db.Column(db.Integer, primary_key=True)
    order_id = db.Column(db.Integer, db.ForeignKey('orders.id'), unique=True, nullable=False)
    partner_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False)

    status = db.Column(db.String(50), default='PENDING', nullable=False)
    # PENDING → ASSIGNED → IN_TRANSIT → DELIVERED

    rider_name = db.Column(db.String(100), nullable=True)
    rider_phone = db.Column(db.String(20), nullable=True)
    tracking_link = db.Column(db.String(255), nullable=True)
    delivery_fee = db.Column(db.Numeric(10, 2), default=0.00)

    assigned_at = db.Column(db.DateTime(timezone=True), nullable=True)
    delivered_at = db.Column(db.DateTime(timezone=True), nullable=True)

    # Real-time coordinates telemetry (polling-based)
    current_latitude = db.Column(db.Numeric(9, 6), nullable=True)
    current_longitude = db.Column(db.Numeric(9, 6), nullable=True)
    location_updated_at = db.Column(db.DateTime(timezone=True), nullable=True)

    created_at = db.Column(db.DateTime(timezone=True), default=utcnow)
    updated_at = db.Column(db.DateTime(timezone=True), default=utcnow, onupdate=utcnow)

    # Relationships
    order = db.relationship('Order')
    partner = db.relationship('User', foreign_keys=[partner_id])
