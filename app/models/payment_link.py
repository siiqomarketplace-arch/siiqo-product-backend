from app.extensions import db
from datetime import datetime

class PaymentLink(db.Model):
    __tablename__ = 'payment_links'
    
    id = db.Column(db.Integer, primary_key=True)
    vendor_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False)
    link_type = db.Column(db.String(20), default='PAY_LINK', nullable=False) # PAY_LINK, INVOICE
    title = db.Column(db.String(100), nullable=False)
    description = db.Column(db.Text, nullable=True)
    amount = db.Column(db.Numeric(10, 2), nullable=True) # Null means open amount (buyer inputs amount)
    buyer_email = db.Column(db.String(120), nullable=True) # Pre-fill email for Invoice links
    status = db.Column(db.String(20), default='ACTIVE', nullable=False) # ACTIVE, PAID, EXPIRED
    slug = db.Column(db.String(100), unique=True, nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    
    vendor = db.relationship('User', foreign_keys=[vendor_id])
    
    def to_dict(self):
        return {
            "id": self.id,
            "vendor_id": self.vendor_id,
            "link_type": self.link_type,
            "title": self.title,
            "description": self.description,
            "amount": str(self.amount) if self.amount else None,
            "buyer_email": self.buyer_email,
            "status": self.status,
            "slug": self.slug,
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }
