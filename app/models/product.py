from app.extensions import db
from datetime import datetime

class Category(db.Model):
    __tablename__ = 'categories'
    
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), unique=True, nullable=False)
    slug = db.Column(db.String(100), unique=True, nullable=False)
    
    products = db.relationship('Product', back_populates='category')

class Catalog(db.Model):
    __tablename__ = 'catalogs'
    
    id = db.Column(db.Integer, primary_key=True)
    vendor_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False)
    name = db.Column(db.String(100), nullable=False)
    description = db.Column(db.Text, nullable=True)
    
    # Relationships
    vendor = db.relationship('User')
    products = db.relationship('Product', back_populates='catalog')

class Product(db.Model):
    __tablename__ = 'products'
    
    id = db.Column(db.Integer, primary_key=True)
    storefront_id = db.Column(db.Integer, db.ForeignKey('storefronts.id'), nullable=False)
    category_id = db.Column(db.Integer, db.ForeignKey('categories.id'), nullable=True)
    catalog_id = db.Column(db.Integer, db.ForeignKey('catalogs.id'), nullable=True)
    
    name = db.Column(db.String(255), nullable=False)
    description = db.Column(db.Text, nullable=False)
    price = db.Column(db.Numeric(10, 2), nullable=False)
    stock_quantity = db.Column(db.Integer, default=0)
    
    images = db.Column(db.JSON, default=list) # Array of image URLs
    is_active = db.Column(db.Boolean, default=True)

    # ── Negotiation fields ──────────────────────────────────────────
    is_negotiable = db.Column(db.Boolean, default=False)
    floor_price   = db.Column(db.Numeric(10, 2), nullable=True)  # hidden minimum; NULL = no floor
    
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    
    # Relationships
    storefront = db.relationship('Storefront', back_populates='products')
    category = db.relationship('Category', back_populates='products')
    catalog = db.relationship('Catalog', back_populates='products')
    reviews = db.relationship('Review', back_populates='product', lazy='dynamic')
