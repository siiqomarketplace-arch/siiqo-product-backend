from app.extensions import db
from datetime import datetime

class Cart(db.Model):
    __tablename__ = 'carts'
    
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('users.id'), unique=True, nullable=False)
    
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    
    # Relationships
    items = db.relationship('CartItem', back_populates='cart', cascade="all, delete-orphan")
    user = db.relationship('User')

class CartItem(db.Model):
    __tablename__ = 'cart_items'
    
    id = db.Column(db.Integer, primary_key=True)
    cart_id = db.Column(db.Integer, db.ForeignKey('carts.id'), nullable=False)
    product_id = db.Column(db.Integer, db.ForeignKey('products.id'), nullable=False)
    quantity = db.Column(db.Integer, default=1, nullable=False)

    # ── Negotiation fields ──────────────────────────────────────────
    negotiated_price  = db.Column(db.Numeric(10, 2), nullable=True)   # agreed price after negotiation
    negotiation_id    = db.Column(db.Integer, db.ForeignKey('negotiation_requests.id'), nullable=True)

    # Relationships
    cart = db.relationship('Cart', back_populates='items')
    product = db.relationship('Product')
    negotiation = db.relationship('NegotiationRequest', foreign_keys=[negotiation_id])

class Order(db.Model):
    __tablename__ = 'orders'
    
    id = db.Column(db.Integer, primary_key=True)
    buyer_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=True)  # Nullable for guest checkout
    vendor_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False)
    
    total_amount = db.Column(db.Numeric(10, 2), nullable=False)
    status = db.Column(db.String(50), default='PENDING', nullable=False) # PENDING, PAID, SHIPPED, DELIVERED, CANCELLED
    
    # Payment method
    payment_method = db.Column(db.String(20), default='ESCROW', nullable=False) # ESCROW, POD, CRYPTO
    
    # Guest checkout fields
    buyer_email = db.Column(db.String(255), nullable=True)
    buyer_name = db.Column(db.String(255), nullable=True)
    buyer_phone = db.Column(db.String(50), nullable=True)
    is_guest = db.Column(db.Boolean, default=False)
    
    # Logistics information
    logistics_provider_id = db.Column(db.String(100), nullable=True) # e.g. "siiqo_partner_1" or "self_pickup"
    logistics_fee = db.Column(db.Numeric(10, 2), default=0.00)
    tracking_number = db.Column(db.String(100), nullable=True)  # Set by vendor when order is shipped

    # Delivery address details
    delivery_address = db.Column(db.String(255), nullable=True)
    delivery_city = db.Column(db.String(100), nullable=True)
    delivery_state = db.Column(db.String(100), nullable=True)
    delivery_phone = db.Column(db.String(20), nullable=True)
    delivery_name = db.Column(db.String(100), nullable=True)
    
    confirmation_email_sent = db.Column(db.Boolean, default=False)

    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    
    payment_link_id = db.Column(db.Integer, db.ForeignKey('payment_links.id'), nullable=True)

    # Phase 0B: currency and the NGN rate at charge time (None until known),
    # and when the order was paid, delivered and released. Set automatically
    # by app/services/foundations.py.
    currency = db.Column(db.String(10), nullable=True, default='NGN')
    fx_rate_to_ngn = db.Column(db.Numeric(18, 6), nullable=True)
    paid_at = db.Column(db.DateTime(timezone=True), nullable=True)
    delivered_at = db.Column(db.DateTime(timezone=True), nullable=True)
    released_at = db.Column(db.DateTime(timezone=True), nullable=True)

    # Where the buyer came from (app/services/attribution.py): utm_source or
    # pay_link / telegram / whatsapp / market / store / direct, and utm_campaign.
    source = db.Column(db.String(40), nullable=True)
    campaign = db.Column(db.String(100), nullable=True)
    # The marketing code the buyer came through (attribution.ref, upper-case).
    ref_code = db.Column(db.String(24), nullable=True, index=True)

    # When the unpaid-order reminder went out (one per order), and how many
    # "please ship" reminders the seller has had (0, 1 or 2).
    payment_reminded_at = db.Column(db.DateTime(timezone=True), nullable=True)
    ship_reminders_sent = db.Column(db.Integer, default=0)

    # Relationships
    buyer = db.relationship('User', foreign_keys=[buyer_id])
    vendor = db.relationship('User', foreign_keys=[vendor_id])
    items = db.relationship('OrderItem', back_populates='order', cascade="all, delete-orphan")
    escrow = db.relationship('EscrowTransaction', back_populates='order', uselist=False)
    payment_link = db.relationship('PaymentLink', backref='orders')

class OrderItem(db.Model):
    __tablename__ = 'order_items'
    
    id = db.Column(db.Integer, primary_key=True)
    order_id = db.Column(db.Integer, db.ForeignKey('orders.id'), nullable=False)
    product_id = db.Column(db.Integer, db.ForeignKey('products.id'), nullable=True)
    
    price_at_purchase = db.Column(db.Numeric(10, 2), nullable=False)
    quantity = db.Column(db.Integer, nullable=False)
    
    # Relationships
    order = db.relationship('Order', back_populates='items')
    product = db.relationship('Product')
