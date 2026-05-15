"""
finance.py — Finance Models
Handles: Invoices, Receipts, Ledger, Inventory, Expenses
"""
from app.extensions import db
from datetime import datetime, timezone
import uuid


def utcnow():
    return datetime.now(timezone.utc)


class Invoice(db.Model):
    __tablename__ = 'invoices'

    id = db.Column(db.Integer, primary_key=True)
    order_id = db.Column(db.Integer, db.ForeignKey('orders.id'), unique=True, nullable=False)
    vendor_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False)
    buyer_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False)

    invoice_number = db.Column(
        db.String(100), unique=True, nullable=False, index=True,
        default=lambda: f"INV-{uuid.uuid4().hex[:8].upper()}"
    )
    pdf_url = db.Column(db.String(255), nullable=True)

    issue_date = db.Column(db.DateTime(timezone=True), default=utcnow)
    due_date = db.Column(db.DateTime(timezone=True), nullable=True)
    status = db.Column(db.String(50), default='ISSUED')
    # ISSUED, PAID, OVERDUE, CANCELLED

    # Relationships
    order = db.relationship('Order')


class Receipt(db.Model):
    __tablename__ = 'receipts'

    id = db.Column(db.Integer, primary_key=True)
    order_id = db.Column(db.Integer, db.ForeignKey('orders.id'), unique=True, nullable=False)

    receipt_number = db.Column(
        db.String(100), unique=True, nullable=False, index=True,
        default=lambda: f"RCP-{uuid.uuid4().hex[:8].upper()}"
    )
    pdf_url = db.Column(db.String(255), nullable=True)
    issued_at = db.Column(db.DateTime(timezone=True), default=utcnow)

    # Relationships
    order = db.relationship('Order')


class Ledger(db.Model):
    """
    Vendor financial ledger. Written to whenever escrow is released,
    a refund is issued, or a fee is charged.
    """
    __tablename__ = 'ledgers'

    id = db.Column(db.Integer, primary_key=True)
    vendor_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False)

    transaction_type = db.Column(db.String(50), nullable=False)
    # CREDIT (payout), DEBIT (refund/fee)
    amount = db.Column(db.Numeric(10, 2), nullable=False)
    currency = db.Column(db.String(10), default='NGN')

    description = db.Column(db.Text, nullable=True)
    reference_id = db.Column(db.String(100), nullable=True)
    # e.g. escrow transaction number

    balance_after = db.Column(db.Numeric(10, 2), nullable=True)
    # Running balance snapshot for easy display

    created_at = db.Column(db.DateTime(timezone=True), default=utcnow)

    # Relationships
    vendor = db.relationship('User', backref='ledger_entries')


class InventoryItem(db.Model):
    """Inventory management for vendors"""
    __tablename__ = 'inventory_items'

    id = db.Column(db.Integer, primary_key=True)
    vendor_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False, index=True)
    product_id = db.Column(db.Integer, db.ForeignKey('products.id'), nullable=True, index=True)
    
    # Product details
    sku = db.Column(db.String(100), nullable=True, index=True)
    barcode = db.Column(db.String(100), nullable=True, index=True)
    name = db.Column(db.String(255), nullable=False)
    description = db.Column(db.Text, nullable=True)
    category = db.Column(db.String(100), nullable=True)
    
    # Stock tracking
    quantity = db.Column(db.Integer, default=0)
    reorder_level = db.Column(db.Integer, default=10)  # Alert when stock reaches this level
    reorder_quantity = db.Column(db.Integer, default=50)  # Suggested reorder amount
    
    # Pricing
    cost_price = db.Column(db.Numeric(10, 2), nullable=True)  # What vendor paid
    selling_price = db.Column(db.Numeric(10, 2), nullable=True)  # What customer pays
    
    # Location
    location = db.Column(db.String(100), nullable=True)  # Warehouse, Store, etc.
    
    # Tracking
    batch_number = db.Column(db.String(100), nullable=True)
    expiry_date = db.Column(db.DateTime(timezone=True), nullable=True)
    
    # Status
    is_active = db.Column(db.Boolean, default=True)
    
    # Timestamps
    created_at = db.Column(db.DateTime(timezone=True), default=utcnow)
    updated_at = db.Column(db.DateTime(timezone=True), default=utcnow, onupdate=utcnow)
    
    # Relationships
    vendor = db.relationship('User', backref='inventory_items')
    product = db.relationship('Product', backref='inventory_item')
    movements = db.relationship('StockMovement', back_populates='item', cascade='all, delete-orphan')
    
    def to_dict(self):
        return {
            'id': self.id,
            'vendor_id': self.vendor_id,
            'product_id': self.product_id,
            'sku': self.sku,
            'barcode': self.barcode,
            'name': self.name,
            'description': self.description,
            'category': self.category,
            'quantity': self.quantity,
            'reorder_level': self.reorder_level,
            'reorder_quantity': self.reorder_quantity,
            'cost_price': str(self.cost_price) if self.cost_price else None,
            'selling_price': str(self.selling_price) if self.selling_price else None,
            'location': self.location,
            'batch_number': self.batch_number,
            'expiry_date': self.expiry_date.isoformat() if self.expiry_date else None,
            'is_active': self.is_active,
            'stock_status': self.get_stock_status(),
            'stock_value': str(self.get_stock_value()) if self.cost_price else None,
            'created_at': self.created_at.isoformat() if self.created_at else None,
            'updated_at': self.updated_at.isoformat() if self.updated_at else None,
        }
    
    def get_stock_status(self):
        """Get stock status: IN_STOCK, LOW_STOCK, OUT_OF_STOCK"""
        if self.quantity <= 0:
            return 'OUT_OF_STOCK'
        elif self.quantity <= self.reorder_level:
            return 'LOW_STOCK'
        else:
            return 'IN_STOCK'
    
    def get_stock_value(self):
        """Calculate total stock value"""
        if self.cost_price:
            return self.quantity * self.cost_price
        return 0


class StockMovement(db.Model):
    """Track all stock movements for audit trail"""
    __tablename__ = 'stock_movements'

    id = db.Column(db.Integer, primary_key=True)
    inventory_item_id = db.Column(db.Integer, db.ForeignKey('inventory_items.id'), nullable=False, index=True)
    
    movement_type = db.Column(db.String(50), nullable=False, index=True)
    # IN (purchase, return, adjustment), OUT (sale, damage, theft), TRANSFER
    
    quantity = db.Column(db.Integer, nullable=False)  # Positive for IN, negative for OUT
    quantity_before = db.Column(db.Integer, nullable=False)
    quantity_after = db.Column(db.Integer, nullable=False)
    
    reference_type = db.Column(db.String(50), nullable=True)  # ORDER, PURCHASE, ADJUSTMENT
    reference_id = db.Column(db.Integer, nullable=True)  # ID of related record
    
    notes = db.Column(db.Text, nullable=True)
    performed_by = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=True)
    
    created_at = db.Column(db.DateTime(timezone=True), default=utcnow, index=True)
    
    # Relationships
    item = db.relationship('InventoryItem', back_populates='movements')
    user = db.relationship('User')


class Expense(db.Model):
    """Track business expenses"""
    __tablename__ = 'expenses'

    id = db.Column(db.Integer, primary_key=True)
    vendor_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False, index=True)
    
    # Expense details
    expense_date = db.Column(db.DateTime(timezone=True), default=utcnow, index=True)
    category = db.Column(db.String(100), nullable=False, index=True)
    # RENT, UTILITIES, SUPPLIES, MARKETING, SALARIES, TRANSPORTATION, MAINTENANCE, INSURANCE, TAXES, OTHER
    
    amount = db.Column(db.Numeric(10, 2), nullable=False)
    currency = db.Column(db.String(10), default='NGN')
    
    payment_method = db.Column(db.String(50), nullable=True)
    # CASH, BANK_TRANSFER, CARD, MOBILE_MONEY
    
    vendor_name = db.Column(db.String(255), nullable=True)  # Supplier/vendor name
    description = db.Column(db.Text, nullable=True)
    receipt_url = db.Column(db.String(255), nullable=True)  # Uploaded receipt
    
    # Recurring expense
    is_recurring = db.Column(db.Boolean, default=False)
    recurrence_frequency = db.Column(db.String(50), nullable=True)  # MONTHLY, QUARTERLY, YEARLY
    
    # Approval workflow
    status = db.Column(db.String(50), default='PENDING', index=True)
    # PENDING, APPROVED, REJECTED
    approved_by = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=True)
    approved_at = db.Column(db.DateTime(timezone=True), nullable=True)
    
    # Tags for categorization
    tags = db.Column(db.JSON, nullable=True)  # Array of tags
    
    # Timestamps
    created_at = db.Column(db.DateTime(timezone=True), default=utcnow)
    updated_at = db.Column(db.DateTime(timezone=True), default=utcnow, onupdate=utcnow)
    
    # Relationships
    vendor = db.relationship('User', foreign_keys=[vendor_id], backref='expenses')
    approver = db.relationship('User', foreign_keys=[approved_by])
    
    def to_dict(self):
        return {
            'id': self.id,
            'vendor_id': self.vendor_id,
            'expense_date': self.expense_date.isoformat() if self.expense_date else None,
            'category': self.category,
            'amount': str(self.amount),
            'currency': self.currency,
            'payment_method': self.payment_method,
            'vendor_name': self.vendor_name,
            'description': self.description,
            'receipt_url': self.receipt_url,
            'is_recurring': self.is_recurring,
            'recurrence_frequency': self.recurrence_frequency,
            'status': self.status,
            'approved_by': self.approved_by,
            'approved_at': self.approved_at.isoformat() if self.approved_at else None,
            'tags': self.tags or [],
            'created_at': self.created_at.isoformat() if self.created_at else None,
            'updated_at': self.updated_at.isoformat() if self.updated_at else None,
        }


class BrandingSettings(db.Model):
    """Store vendor branding settings for invoices/receipts"""
    __tablename__ = 'branding_settings'

    id = db.Column(db.Integer, primary_key=True)
    vendor_id = db.Column(db.Integer, db.ForeignKey('users.id'), unique=True, nullable=False)
    
    # Logo and images
    logo_url = db.Column(db.String(255), nullable=True)
    
    # Colors
    primary_color = db.Column(db.String(7), default='#0b1b3b')  # Hex color
    secondary_color = db.Column(db.String(7), default='#E0921C')  # Hex color
    accent_color = db.Column(db.String(7), default='#FFFFFF')  # Hex color
    
    # Typography
    font_family = db.Column(db.String(100), default='Inter')
    
    # Invoice settings
    invoice_prefix = db.Column(db.String(20), default='INV')
    invoice_next_number = db.Column(db.Integer, default=1)
    invoice_template = db.Column(db.String(50), default='MODERN')  # MODERN, CLASSIC, MINIMAL
    
    # Receipt settings
    receipt_prefix = db.Column(db.String(20), default='RCP')
    receipt_next_number = db.Column(db.Integer, default=1)
    receipt_template = db.Column(db.String(50), default='MODERN')
    
    # Business details
    business_address = db.Column(db.Text, nullable=True)
    business_phone = db.Column(db.String(20), nullable=True)
    business_email = db.Column(db.String(255), nullable=True)
    business_website = db.Column(db.String(255), nullable=True)
    tax_id = db.Column(db.String(100), nullable=True)
    
    # Payment terms
    default_payment_terms = db.Column(db.String(100), default='Due on receipt')
    default_due_days = db.Column(db.Integer, default=0)
    
    # Footer text
    invoice_footer = db.Column(db.Text, nullable=True)
    receipt_footer = db.Column(db.Text, nullable=True)
    
    # JSON store for standalone templates and documents
    template_options = db.Column(db.JSON, nullable=True)
    
    # Timestamps
    created_at = db.Column(db.DateTime(timezone=True), default=utcnow)
    updated_at = db.Column(db.DateTime(timezone=True), default=utcnow, onupdate=utcnow)
    
    # Relationships
    vendor = db.relationship('User', backref='branding_settings')
    
    def to_dict(self):
        return {
            'id': self.id,
            'vendor_id': self.vendor_id,
            'logo_url': self.logo_url,
            'primary_color': self.primary_color,
            'secondary_color': self.secondary_color,
            'accent_color': self.accent_color,
            'font_family': self.font_family,
            'invoice_prefix': self.invoice_prefix,
            'invoice_next_number': self.invoice_next_number,
            'invoice_template': self.invoice_template,
            'receipt_prefix': self.receipt_prefix,
            'receipt_next_number': self.receipt_next_number,
            'receipt_template': self.receipt_template,
            'business_address': self.business_address,
            'business_phone': self.business_phone,
            'business_email': self.business_email,
            'business_website': self.business_website,
            'tax_id': self.tax_id,
            'default_payment_terms': self.default_payment_terms,
            'default_due_days': self.default_due_days,
            'invoice_footer': self.invoice_footer,
            'receipt_footer': self.receipt_footer,
        }

