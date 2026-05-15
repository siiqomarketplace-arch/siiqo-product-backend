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
    buyer_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False)
    vendor_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False)
    
    total_amount = db.Column(db.Numeric(10, 2), nullable=False)
    status = db.Column(db.String(50), default='PENDING', nullable=False) # PENDING, PAID, SHIPPED, DELIVERED, CANCELLED
    
    # Payment method
    payment_method = db.Column(db.String(20), default='ESCROW', nullable=False) # ESCROW, POD, CRYPTO
    
    # Logistics information
    logistics_provider_id = db.Column(db.String(100), nullable=True) # e.g. "siiqo_partner_1" or "self_pickup"
    logistics_fee = db.Column(db.Numeric(10, 2), default=0.00)
    
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    
    # Relationships
    buyer = db.relationship('User', foreign_keys=[buyer_id])
    vendor = db.relationship('User', foreign_keys=[vendor_id])
    items = db.relationship('OrderItem', back_populates='order', cascade="all, delete-orphan")
    escrow = db.relationship('EscrowTransaction', back_populates='order', uselist=False)

class OrderItem(db.Model):
    __tablename__ = 'order_items'
    
    id = db.Column(db.Integer, primary_key=True)
    order_id = db.Column(db.Integer, db.ForeignKey('orders.id'), nullable=False)
    product_id = db.Column(db.Integer, db.ForeignKey('products.id'), nullable=False)
    
    price_at_purchase = db.Column(db.Numeric(10, 2), nullable=False)
    quantity = db.Column(db.Integer, nullable=False)
    
    # Relationships
    order = db.relationship('Order', back_populates='items')
    product = db.relationship('Product')
