from app.extensions import db
from datetime import datetime

class CustomerProfile(db.Model):
    __tablename__ = 'customer_profiles'
    
    id = db.Column(db.Integer, primary_key=True)
    vendor_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False)
    buyer_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False)
    
    total_spent = db.Column(db.Numeric(10, 2), default=0.00)
    total_orders = db.Column(db.Integer, default=0)
    segment = db.Column(db.String(50), default='NEW') # NEW, REGULAR, VIP, AT_RISK
    
    last_purchase_date = db.Column(db.DateTime, nullable=True)
    
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    
    # Unique constraint: a buyer is a customer of a vendor only once in this table
    __table_args__ = (db.UniqueConstraint('vendor_id', 'buyer_id', name='_vendor_buyer_uc'),)
    
    # Relationships
    vendor = db.relationship('User', foreign_keys=[vendor_id])
    buyer = db.relationship('User', foreign_keys=[buyer_id])
