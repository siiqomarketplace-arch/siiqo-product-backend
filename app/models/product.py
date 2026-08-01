from app.extensions import db
from datetime import datetime

class Category(db.Model):
    __tablename__ = 'categories'
    
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), unique=True, nullable=False)
    slug = db.Column(db.String(100), unique=True, nullable=False)

    # ── NEW: attribute schema for category-specific listing fields ──────────
    # JSON array of field descriptors. NULL = no extra fields (backward compat).
    # Example: [{"key":"size","label":"Size","type":"multiselect","options":["XS","S","M","L","XL"]}]
    attribute_schema = db.Column(db.JSON, nullable=True)

    # ── NEW: hint for which product types this category applies to ──────────
    # Example: ["physical"] or ["digital"] or ["physical","service"]
    # NULL means it applies to all types (backward compat).
    product_type_hint = db.Column(db.JSON, nullable=True)

    # ── NEW: icon name for UI display (e.g. "Shirt", "Cpu", "Home") ─────────
    icon = db.Column(db.String(50), nullable=True)
    
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
    is_deleted = db.Column(db.Boolean, default=False)
    view_count = db.Column(db.Integer, default=0)

    # ── Condition & Location ─────────────────────────────────────────
    condition = db.Column(db.String(50), default='New')
    location = db.Column(db.String(255), nullable=True)
    latitude = db.Column(db.Float, nullable=True)
    longitude = db.Column(db.Float, nullable=True)

    # ── Negotiation fields ──────────────────────────────────────────
    is_negotiable = db.Column(db.Boolean, default=False)
    floor_price   = db.Column(db.Numeric(10, 2), nullable=True)  # hidden minimum; NULL = no floor

    # ── Product type & digital/service fields ────────────────────────
    product_type  = db.Column(db.String(20), default='physical')   # physical | digital | service
    file_url      = db.Column(db.String(500), nullable=True)       # download link for digital
    booking_link  = db.Column(db.String(500), nullable=True)       # booking URL for services

    # ── Inventory / SEO extras ───────────────────────────────────────
    sku              = db.Column(db.String(100), nullable=True)
    weight           = db.Column(db.Numeric(8, 2), nullable=True)  # kg
    seo_title        = db.Column(db.String(255), nullable=True)
    seo_description  = db.Column(db.Text, nullable=True)

    # ── Category-specific attributes (additive, non-breaking) ───────────────
    # Stored as JSON. Existing products have NULL here — renders as nothing.
    # Example: {"color": "Blue", "size": "XL", "material": "Cotton"}
    attributes = db.Column(db.JSON, nullable=True)

    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    
    # Relationships
    storefront = db.relationship('Storefront', back_populates='products')
    category = db.relationship('Category', back_populates='products')
    catalog = db.relationship('Catalog', back_populates='products')
    reviews = db.relationship('Review', back_populates='product', lazy='dynamic')
