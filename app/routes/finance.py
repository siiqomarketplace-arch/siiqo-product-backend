import logging
"""
finance.py — Finance Tools Routes
Handles: Invoices (standalone), Receipts (standalone), Customers (CRM),
         Summary, Payment links, Inventory, Expenses, Branding, Reports
"""
from flask import Blueprint, request, jsonify
from flask_jwt_extended import jwt_required, get_jwt_identity
from app.extensions import db, limiter
from app.models.finance import InventoryItem, StockMovement, Expense, BrandingSettings, Ledger, Invoice, Receipt
from app.models.user import User, Storefront
from app.models.order import Order
from app.models.product import Product
from app.models.crm import CustomerProfile
from datetime import datetime, timezone, timedelta
from sqlalchemy import func, and_
import uuid

finance_bp = Blueprint('finance', __name__, url_prefix='/api/finance')


def utcnow():
    return datetime.now(timezone.utc)


def _require_vendor(user_id: int):
    """Helper to check if user is a vendor"""
    user = User.query.get(user_id)
    if not user or user.role not in ('VENDOR', 'PARTNER', 'ADMIN'):
        return None
    return user


# ---------------------------------------------------------------------------
# STANDALONE INVOICES — not tied to orders
# ---------------------------------------------------------------------------

# In-memory store for standalone invoices (persisted in BrandingSettings.invoice_next_number)
# We use a simple JSON column on BrandingSettings to store standalone docs.
# For a production system these would be their own DB table; here we store them
# in the vendor's BrandingSettings.template_options as a lightweight solution.

def _get_or_create_branding(user_id: int) -> BrandingSettings:
    settings = BrandingSettings.query.filter_by(vendor_id=user_id).first()
    if not settings:
        settings = BrandingSettings(vendor_id=user_id)
        db.session.add(settings)
        db.session.flush()
    return settings


def _next_invoice_number(settings: BrandingSettings) -> str:
    prefix = settings.invoice_prefix or 'INV'
    num = settings.invoice_next_number or 1
    settings.invoice_next_number = num + 1
    return f"{prefix}-{num:04d}"


def _next_receipt_number(settings: BrandingSettings) -> str:
    prefix = settings.receipt_prefix or 'RCP'
    num = settings.receipt_next_number or 1
    settings.receipt_next_number = num + 1
    return f"{prefix}-{num:04d}"


from sqlalchemy.orm.attributes import flag_modified
def _get_standalone_docs(user_id: int, doc_type: str) -> list:
    """Read standalone invoices or receipts from DB directly."""
    if doc_type == 'invoice':
        docs = Invoice.query.filter_by(vendor_id=user_id, order_id=None).order_by(Invoice.issue_date.desc()).all()
    else:
        docs = Receipt.query.filter_by(vendor_id=user_id, order_id=None).order_by(Receipt.issued_at.desc()).all()
    return [d.to_dict() for d in docs]


@finance_bp.route('/invoices', methods=['GET'])
@jwt_required()
@limiter.limit("60 per minute")
def list_invoices():
    """List all standalone invoices for the logged-in user"""
    user_id = get_jwt_identity()
    status_filter = request.args.get('status')
    
    query = Invoice.query.filter_by(vendor_id=user_id, order_id=None)
    if status_filter:
        query = query.filter_by(status=status_filter)
    
    invoices = query.order_by(Invoice.issue_date.desc()).all()
    invoices_data = [inv.to_dict() for inv in invoices]
    
    return jsonify({
        'status': 'success',
        'invoices': invoices_data,
        'total': len(invoices_data)
    }), 200


@finance_bp.route('/invoices', methods=['POST'])
@jwt_required()
@limiter.limit("30 per minute")
def create_invoice():
    """Create a new standalone invoice"""
    user_id = get_jwt_identity()
    data = request.get_json() or {}

    customer_name = (data.get('customer_name') or '').strip()
    if not customer_name:
        return jsonify({'message': 'customer_name is required'}), 400

    line_items = data.get('line_items', [])
    if not line_items:
        return jsonify({'message': 'At least one line item is required'}), 400

    # Calculate totals
    subtotal = sum(float(item.get('qty', 1)) * float(item.get('price', 0)) for item in line_items)
    discount = float(data.get('discount', 0))
    tax_rate = float(data.get('tax_rate', 0))
    tax_amount = round((subtotal - discount) * tax_rate / 100, 2)
    total = round(subtotal - discount + tax_amount, 2)

    settings = _get_or_create_branding(user_id)
    invoice_number = _next_invoice_number(settings)

    due_date_str = data.get('due_date')
    due_date = None
    if due_date_str:
        try:
            if 'T' in due_date_str:
                due_date = datetime.fromisoformat(due_date_str.replace('Z', '+00:00'))
            else:
                due_date = datetime.strptime(due_date_str, '%Y-%m-%d')
        except Exception:
            pass

    invoice = Invoice(
        vendor_id=user_id,
        invoice_number=invoice_number,
        customer_name=customer_name,
        customer_email=data.get('customer_email', ''),
        customer_phone=data.get('customer_phone', ''),
        customer_address=data.get('customer_address', ''),
        line_items=line_items,
        subtotal=subtotal,
        discount=discount,
        tax_rate=tax_rate,
        tax_amount=tax_amount,
        total=total,
        currency=data.get('currency', 'NGN'),
        notes=data.get('notes', ''),
        due_date=due_date,
        status='draft',
        payment_link_token=str(uuid.uuid4()),
    )
    
    db.session.add(invoice)
    db.session.commit()

    return jsonify({
        'status': 'success',
        'message': 'Invoice created successfully',
        'invoice': invoice.to_dict()
    }), 201


@finance_bp.route('/invoices/<string:invoice_id>', methods=['GET'])
@jwt_required()
def get_invoice(invoice_id):
    """Get a single standalone invoice"""
    user_id = get_jwt_identity()
    invoice = Invoice.query.filter(
        Invoice.vendor_id == user_id,
        db.or_(Invoice.id == invoice_id, Invoice.payment_link_token == invoice_id, Invoice.invoice_number == invoice_id)
    ).first()
    
    if not invoice:
        # scan for UUID string representation
        invoices = Invoice.query.filter_by(vendor_id=user_id).all()
        invoice = next((inv for inv in invoices if str(inv.id) == str(invoice_id)), None)
        
    if not invoice:
        return jsonify({'message': 'Invoice not found'}), 404
        
    return jsonify({'status': 'success', 'invoice': invoice.to_dict()}), 200


@finance_bp.route('/invoices/<string:invoice_id>/status', methods=['PATCH'])
@jwt_required()
def update_invoice_status(invoice_id):
    """Update invoice status (paid, sent, cancelled, etc.)"""
    user_id = get_jwt_identity()
    data = request.get_json() or {}
    new_status = data.get('status')

    valid_statuses = ('paid', 'sent', 'draft', 'overdue', 'cancelled')
    if new_status not in valid_statuses:
        return jsonify({'message': f'Invalid status. Must be one of: {", ".join(valid_statuses)}'}), 400

    invoices = Invoice.query.filter_by(vendor_id=user_id).all()
    invoice = next((inv for inv in invoices if str(inv.id) == str(invoice_id)), None)
    
    if not invoice:
        return jsonify({'message': 'Invoice not found'}), 404

    invoice.status = new_status
    if new_status == 'paid' and data.get('payment_method'):
        invoice.payment_method = data['payment_method']
        
    db.session.commit()

    return jsonify({'status': 'success', 'message': f'Invoice marked as {new_status}'}), 200


# ---------------------------------------------------------------------------
# STANDALONE RECEIPTS
# ---------------------------------------------------------------------------

@finance_bp.route('/receipts', methods=['GET'])
@jwt_required()
@limiter.limit("60 per minute")
def list_receipts():
    """List all standalone receipts for the logged-in user"""
    user_id = get_jwt_identity()
    receipts = Receipt.query.filter_by(vendor_id=user_id, order_id=None).order_by(Receipt.issued_at.desc()).all()
    receipts_data = [r.to_dict() for r in receipts]
    return jsonify({
        'status': 'success',
        'receipts': receipts_data,
        'total': len(receipts_data)
    }), 200


@finance_bp.route('/receipts/<string:receipt_id>', methods=['GET'])
@jwt_required()
def get_receipt(receipt_id):
    """Get a single standalone receipt"""
    user_id = get_jwt_identity()
    receipts = Receipt.query.filter_by(vendor_id=user_id, order_id=None).all()
    receipt = next((r for r in receipts if str(r.id) == str(receipt_id)), None)
    
    if not receipt:
        return jsonify({'message': 'Receipt not found'}), 404
    return jsonify({'status': 'success', 'receipt': receipt.to_dict()}), 200


@finance_bp.route('/receipts', methods=['POST'])
@jwt_required()
@limiter.limit("30 per minute")
def create_receipt():
    """Create a new standalone receipt with atomic backend stock sync"""
    user_id = get_jwt_identity()
    data = request.get_json() or {}

    customer_name = (data.get('customer_name') or '').strip()
    if not customer_name:
        return jsonify({'message': 'customer_name is required'}), 400

    line_items = data.get('line_items', [])
    subtotal = sum(float(item.get('qty', 1)) * float(item.get('price', 0)) for item in line_items)
    tax_amount = float(data.get('tax_amount', 0))
    discount = float(data.get('discount', 0))
    total = round(subtotal + tax_amount - discount, 2)

    settings = _get_or_create_branding(user_id)
    receipt_number = _next_receipt_number(settings)

    receipt = Receipt(
        vendor_id=user_id,
        receipt_number=receipt_number,
        customer_name=customer_name,
        customer_email=data.get('customer_email', ''),
        customer_phone=data.get('customer_phone', ''),
        line_items=line_items,
        subtotal=subtotal,
        tax_amount=tax_amount,
        discount=discount,
        total=total,
        currency=data.get('currency', 'NGN'),
        payment_method=data.get('payment_method', 'Cash'),
        notes=data.get('notes', ''),
        status='paid',
    )

    db.session.add(receipt)
    db.session.flush() # get receipt.id

    # Atomic backend stock sync (Task 4)
    for item in line_items:
        desc = item.get('description', '').strip()
        qty = int(item.get('qty', 1))
        
        # 1. Deduct from back-office InventoryItem first
        inv_item = InventoryItem.query.filter(
            InventoryItem.vendor_id == user_id,
            InventoryItem.is_active == True,
            InventoryItem.name.ilike(desc)
        ).first()
        
        if inv_item:
            qty_before = inv_item.quantity
            qty_after = max(0, qty_before - qty)
            inv_item.quantity = qty_after
            inv_item.updated_at = utcnow()
            
            # Create StockMovement record
            movement = StockMovement(
                inventory_item_id=inv_item.id,
                movement_type='OUT',
                quantity=-qty,
                quantity_before=qty_before,
                quantity_after=qty_after,
                reference_type='RECEIPT',
                reference_id=receipt.id,
                notes=f"Offline receipt sale: {receipt_number}",
                performed_by=user_id
            )
            db.session.add(movement)
            
            # 2. Also deduct from associated storefront Product if linked
            if inv_item.product_id:
                prod = Product.query.get(inv_item.product_id)
                if prod:
                    prod.stock_quantity = max(0, prod.stock_quantity - qty)
        else:
            # 3. If no InventoryItem but storefront Product matches by name directly
            from app.models.user import Storefront
            sf = Storefront.query.filter_by(vendor_id=user_id).first()
            if sf:
                prod = Product.query.filter(
                    Product.storefront_id == sf.id,
                    Product.name.ilike(desc)
                ).first()
                if prod:
                    prod.stock_quantity = max(0, prod.stock_quantity - qty)

    db.session.commit()

    return jsonify({
        'status': 'success',
        'message': 'Receipt created successfully',
        'receipt': receipt.to_dict()
    }), 201


# ---------------------------------------------------------------------------
# FINANCE SUMMARY — dashboard stats
# ---------------------------------------------------------------------------

@finance_bp.route('/summary', methods=['GET'])
@jwt_required()
@limiter.limit("60 per minute")
def get_finance_summary():
    """Get finance summary stats for the dashboard"""
    user_id = get_jwt_identity()

    # Query invoices & receipts directly from the DB tables instead of JSON!
    invoices = Invoice.query.filter_by(vendor_id=user_id, order_id=None).all()
    receipts = Receipt.query.filter_by(vendor_id=user_id, order_id=None).all()

    paid_invoices = [i for i in invoices if i.status == 'paid']
    pending_invoices = [i for i in invoices if i.status in ('draft', 'sent')]
    overdue_invoices = [i for i in invoices if i.status == 'overdue']

    total_invoiced = sum(float(i.total or 0) for i in invoices)
    total_paid = sum(float(i.total or 0) for i in paid_invoices)
    total_pending = sum(float(i.total or 0) for i in pending_invoices)
    total_receipt_revenue = sum(float(r.total or 0) for r in receipts)

    # CRM customers
    customer_count = CustomerProfile.query.filter_by(vendor_id=user_id).count()

    # Ledger revenue (from escrow releases)
    ledger_revenue = db.session.query(func.sum(Ledger.amount)).filter_by(
        vendor_id=user_id, transaction_type='CREDIT'
    ).scalar() or 0

    total_revenue = float(ledger_revenue) + total_receipt_revenue

    return jsonify({
        'status': 'success',
        'summary': {
            'invoices': {
                'total': len(invoices),
                'paid': len(paid_invoices),
                'pending': len(pending_invoices),
                'overdue': len(overdue_invoices),
                'total_invoiced': total_invoiced,
                'total_paid': total_paid,
                'total_pending': total_pending,
            },
            'receipts': {
                'total': len(receipts),
                'total_revenue': total_receipt_revenue,
            },
            'customers': {
                'total': customer_count,
            },
            'total_revenue': total_revenue,
        }
    }), 200


# ---------------------------------------------------------------------------
# CUSTOMERS — CRM
# ---------------------------------------------------------------------------

@finance_bp.route('/customers', methods=['GET'])
@jwt_required()
@limiter.limit("60 per minute")
def get_customers():
    """Get CRM customers for the logged-in vendor"""
    user_id = get_jwt_identity()
    profiles = CustomerProfile.query.filter_by(vendor_id=user_id).all()
    customers = [{
        'id': p.buyer_id,
        'buyer_id': p.buyer_id,
        'name': p.buyer.full_name if p.buyer else 'Unknown',
        'email': p.buyer.email if p.buyer else '',
        'total_spent': str(p.total_spent),
        'total_orders': p.total_orders,
        'segment': p.segment,
        'last_purchase_date': p.last_purchase_date.isoformat() if p.last_purchase_date else None,
    } for p in profiles]
    return jsonify({
        'status': 'success',
        'customers': customers,
        'total': len(customers)
    }), 200


# ---------------------------------------------------------------------------
# PAYMENT LINK — public, no auth
# ---------------------------------------------------------------------------

@finance_bp.route('/pay/<string:token>', methods=['GET'])
@limiter.limit("30 per minute")
def get_payment_link(token):
    """Public payment link — look up invoice by payment_link_token"""
    invoice = Invoice.query.filter_by(payment_link_token=token).first()
    if invoice:
        return jsonify({
            'status': 'success',
            'invoice': invoice.to_dict()
        }), 200
    return jsonify({'message': 'Payment link not found or expired'}), 404


# ---------------------------------------------------------------------------
# REVENUE STATUS — subscription & usage check
# ---------------------------------------------------------------------------

@finance_bp.route('/revenue/status', methods=['GET'])
@limiter.limit("60 per minute")
def revenue_status():
    """Check subscription status and monthly usage (public or authenticated)"""
    user_id = None
    try:
        from flask_jwt_extended import verify_jwt_in_request, get_jwt_identity
        verify_jwt_in_request(optional=True)
        user_id = get_jwt_identity()
    except Exception:
        pass

    if not user_id:
        # Guest: show free tier limits
        return jsonify({
            'status': 'success',
            'has_active_subscription': False,
            'plan_type': 'FREE',
            'usage': {'current_month_invoices': 0, 'limit': 6}
        }), 200

    # Count this month's standalone invoices + receipts directly from the DB tables instead of JSON!
    now = datetime.now(timezone.utc)
    month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    
    month_invoices = Invoice.query.filter(
        Invoice.vendor_id == user_id,
        Invoice.order_id == None,
        Invoice.issue_date >= month_start
    ).count()
    
    month_receipts = Receipt.query.filter(
        Receipt.vendor_id == user_id,
        Receipt.order_id == None,
        Receipt.issued_at >= month_start
    ).count()
    
    month_count = month_invoices + month_receipts

    # Check for an active subscription in the database
    from app.models.admin import VendorSubscription
    now = utcnow()
    active_sub = VendorSubscription.query.filter(
        VendorSubscription.vendor_id == int(user_id),
        VendorSubscription.status == 'ACTIVE',
        VendorSubscription.end_date > now,
    ).first()

    has_sub = active_sub is not None
    plan_type = 'FREE'
    if has_sub and active_sub.plan:
        plan_type = active_sub.plan.name

    return jsonify({
        'status': 'success',
        'has_active_subscription': has_sub,
        'plan_type': plan_type,
        'usage': {
            'current_month_invoices': month_count,
            'limit': None if has_sub else 6,  # None = unlimited for Pro users
        }
    }), 200


# ---------------------------------------------------------------------------
# INVENTORY — Manage inventory items
# ---------------------------------------------------------------------------

@finance_bp.route('/inventory', methods=['GET'])
@jwt_required()
@limiter.limit("60 per minute")
def get_inventory():
    """Get all inventory items for vendor"""
    user_id = get_jwt_identity()
    user = _require_vendor(user_id)
    if not user:
        return jsonify({'message': 'Vendor access required'}), 403
    
    page = int(request.args.get('page', 1))
    per_page = min(int(request.args.get('per_page', 50)), 100)
    status = request.args.get('status')  # all, in_stock, low_stock, out_of_stock
    category = request.args.get('category')
    search = request.args.get('search', '').strip()
    
    # Base query
    query = InventoryItem.query.filter_by(vendor_id=user_id, is_active=True)
    
    # Filter by status
    if status == 'out_of_stock':
        query = query.filter(InventoryItem.quantity <= 0)
    elif status == 'low_stock':
        query = query.filter(
            and_(
                InventoryItem.quantity > 0,
                InventoryItem.quantity <= InventoryItem.reorder_level
            )
        )
    elif status == 'in_stock':
        query = query.filter(InventoryItem.quantity > InventoryItem.reorder_level)
    
    # Filter by category
    if category:
        query = query.filter_by(category=category)
    
    # Search
    if search:
        query = query.filter(
            db.or_(
                InventoryItem.name.ilike(f'%{search}%'),
                InventoryItem.sku.ilike(f'%{search}%'),
                InventoryItem.barcode.ilike(f'%{search}%')
            )
        )
    
    # Order by name
    query = query.order_by(InventoryItem.name.asc())
    
    # Paginate
    paginated = query.paginate(page=page, per_page=per_page, error_out=False)
    
    items = [item.to_dict() for item in paginated.items]
    
    # Calculate summary
    total_items = InventoryItem.query.filter_by(vendor_id=user_id, is_active=True).count()
    low_stock = InventoryItem.query.filter(
        InventoryItem.vendor_id == user_id,
        InventoryItem.is_active == True,
        InventoryItem.quantity > 0,
        InventoryItem.quantity <= InventoryItem.reorder_level
    ).count()
    out_of_stock = InventoryItem.query.filter(
        InventoryItem.vendor_id == user_id,
        InventoryItem.is_active == True,
        InventoryItem.quantity <= 0
    ).count()
    
    # Calculate total stock value
    total_value = db.session.query(
        func.sum(InventoryItem.quantity * InventoryItem.cost_price)
    ).filter(
        InventoryItem.vendor_id == user_id,
        InventoryItem.is_active == True,
        InventoryItem.cost_price.isnot(None)
    ).scalar() or 0
    
    return jsonify({
        'items': items,
        'total': paginated.total,
        'page': page,
        'per_page': per_page,
        'pages': paginated.pages,
        'summary': {
            'total_items': total_items,
            'low_stock': low_stock,
            'out_of_stock': out_of_stock,
            'total_value': str(total_value)
        }
    }), 200


@finance_bp.route('/inventory', methods=['POST'])
@jwt_required()
@limiter.limit("30 per minute")
def add_inventory_item():
    """Add a new inventory item"""
    user_id = get_jwt_identity()
    user = _require_vendor(user_id)
    if not user:
        return jsonify({'message': 'Vendor access required'}), 403
    
    data = request.get_json() or {}
    
    # Validate
    name = (data.get('name') or '').strip()
    if not name:
        return jsonify({'message': 'Name is required'}), 400
    
    # Create item
    item = InventoryItem(
        vendor_id=user_id,
        product_id=data.get('product_id'),
        sku=data.get('sku'),
        barcode=data.get('barcode'),
        name=name,
        description=data.get('description'),
        category=data.get('category'),
        quantity=int(data.get('quantity', 0)),
        reorder_level=int(data.get('reorder_level', 10)),
        reorder_quantity=int(data.get('reorder_quantity', 50)),
        cost_price=data.get('cost_price'),
        selling_price=data.get('selling_price'),
        location=data.get('location'),
        batch_number=data.get('batch_number'),
        expiry_date=data.get('expiry_date')
    )
    
    db.session.add(item)
    db.session.flush()  # Get item.id
    
    # Create initial stock movement
    if item.quantity > 0:
        movement = StockMovement(
            inventory_item_id=item.id,
            movement_type='IN',
            quantity=item.quantity,
            quantity_before=0,
            quantity_after=item.quantity,
            reference_type='INITIAL',
            notes='Initial stock',
            performed_by=user_id
        )
        db.session.add(movement)
    
    db.session.commit()
    
    return jsonify({
        'message': 'Inventory item added',
        'item': item.to_dict()
    }), 201


@finance_bp.route('/inventory/<int:item_id>', methods=['GET'])
@jwt_required()
def get_inventory_item(item_id):
    """Get a single inventory item with movement history"""
    user_id = get_jwt_identity()
    item = InventoryItem.query.filter_by(id=item_id, vendor_id=user_id, is_active=True).first()
    
    if not item:
        return jsonify({'message': 'Item not found'}), 404
    
    # Get recent movements
    movements = StockMovement.query.filter_by(
        inventory_item_id=item_id
    ).order_by(StockMovement.created_at.desc()).limit(20).all()
    
    movements_data = [{
        'id': m.id,
        'movement_type': m.movement_type,
        'quantity': m.quantity,
        'quantity_before': m.quantity_before,
        'quantity_after': m.quantity_after,
        'reference_type': m.reference_type,
        'reference_id': m.reference_id,
        'notes': m.notes,
        'performed_by': m.performed_by,
        'created_at': m.created_at.isoformat() if m.created_at else None
    } for m in movements]
    
    return jsonify({
        'item': item.to_dict(),
        'movements': movements_data
    }), 200


@finance_bp.route('/inventory/<int:item_id>', methods=['PATCH'])
@jwt_required()
def update_inventory_item(item_id):
    """Update inventory item"""
    user_id = get_jwt_identity()
    item = InventoryItem.query.filter_by(id=item_id, vendor_id=user_id, is_active=True).first()
    
    if not item:
        return jsonify({'message': 'Item not found'}), 404
    
    data = request.get_json() or {}
    
    # Update allowed fields
    if 'name' in data:
        item.name = data['name'].strip()
    if 'description' in data:
        item.description = data['description']
    if 'category' in data:
        item.category = data['category']
    if 'sku' in data:
        item.sku = data['sku']
    if 'barcode' in data:
        item.barcode = data['barcode']
    if 'reorder_level' in data:
        item.reorder_level = int(data['reorder_level'])
    if 'reorder_quantity' in data:
        item.reorder_quantity = int(data['reorder_quantity'])
    if 'cost_price' in data:
        item.cost_price = data['cost_price']
    if 'selling_price' in data:
        item.selling_price = data['selling_price']
    if 'location' in data:
        item.location = data['location']
    if 'batch_number' in data:
        item.batch_number = data['batch_number']
    if 'expiry_date' in data:
        item.expiry_date = data['expiry_date']
    
    item.updated_at = utcnow()
    db.session.commit()
    
    return jsonify({
        'message': 'Item updated',
        'item': item.to_dict()
    }), 200


@finance_bp.route('/inventory/<int:item_id>/adjust', methods=['POST'])
@jwt_required()
@limiter.limit("30 per minute")
def adjust_stock(item_id):
    """Adjust stock quantity (add or remove)"""
    user_id = get_jwt_identity()
    item = InventoryItem.query.filter_by(id=item_id, vendor_id=user_id, is_active=True).first()
    
    if not item:
        return jsonify({'message': 'Item not found'}), 404
    
    data = request.get_json() or {}
    
    movement_type = data.get('movement_type')  # IN or OUT
    quantity = int(data.get('quantity', 0))
    notes = data.get('notes', '')
    reference_type = data.get('reference_type', 'ADJUSTMENT')
    reference_id = data.get('reference_id')
    
    if movement_type not in ('IN', 'OUT'):
        return jsonify({'message': 'Invalid movement_type (must be IN or OUT)'}), 400
    
    if quantity <= 0:
        return jsonify({'message': 'Quantity must be positive'}), 400
    
    # Calculate new quantity
    quantity_before = item.quantity
    if movement_type == 'IN':
        quantity_after = quantity_before + quantity
        quantity_change = quantity
    else:  # OUT
        quantity_after = max(0, quantity_before - quantity)
        quantity_change = -quantity
    
    # Update item
    item.quantity = quantity_after
    item.updated_at = utcnow()
    
    # Create movement record
    movement = StockMovement(
        inventory_item_id=item_id,
        movement_type=movement_type,
        quantity=quantity_change,
        quantity_before=quantity_before,
        quantity_after=quantity_after,
        reference_type=reference_type,
        reference_id=reference_id,
        notes=notes,
        performed_by=user_id
    )
    db.session.add(movement)
    db.session.commit()
    
    return jsonify({
        'message': 'Stock adjusted',
        'item': item.to_dict(),
        'movement': {
            'type': movement_type,
            'quantity': quantity,
            'quantity_before': quantity_before,
            'quantity_after': quantity_after
        }
    }), 200


@finance_bp.route('/inventory/<int:item_id>', methods=['DELETE'])
@jwt_required()
def delete_inventory_item(item_id):
    """Delete inventory item (soft delete)"""
    user_id = get_jwt_identity()
    item = InventoryItem.query.filter_by(id=item_id, vendor_id=user_id).first()
    
    if not item:
        return jsonify({'message': 'Item not found'}), 404
    
    item.is_active = False
    db.session.commit()
    
    return jsonify({'message': 'Item deleted'}), 200


# ---------------------------------------------------------------------------
# EXPENSES — Manage expenses
# ---------------------------------------------------------------------------

@finance_bp.route('/expenses', methods=['GET'])
@jwt_required()
@limiter.limit("60 per minute")
def get_expenses():
    """Get all expenses for vendor"""
    user_id = get_jwt_identity()
    user = _require_vendor(user_id)
    if not user:
        return jsonify({'message': 'Vendor access required'}), 403
    
    page = int(request.args.get('page', 1))
    per_page = min(int(request.args.get('per_page', 50)), 100)
    category = request.args.get('category')
    status = request.args.get('status')
    start_date = request.args.get('start_date')
    end_date = request.args.get('end_date')
    
    # Base query
    query = Expense.query.filter_by(vendor_id=user_id)
    
    # Filter by category
    if category:
        query = query.filter_by(category=category)
    
    # Filter by status
    if status:
        query = query.filter_by(status=status)
    
    # Filter by date range
    if start_date:
        query = query.filter(Expense.expense_date >= start_date)
    if end_date:
        query = query.filter(Expense.expense_date <= end_date)
    
    # Order by date desc
    query = query.order_by(Expense.expense_date.desc())
    
    # Paginate
    paginated = query.paginate(page=page, per_page=per_page, error_out=False)
    
    expenses = [exp.to_dict() for exp in paginated.items]
    
    # Calculate summary
    total_amount = db.session.query(
        func.sum(Expense.amount)
    ).filter(
        Expense.vendor_id == user_id,
        Expense.status.in_(['APPROVED', 'PENDING'])
    ).scalar() or 0
    
    # This month
    now = datetime.now(timezone.utc)
    month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    month_amount = db.session.query(
        func.sum(Expense.amount)
    ).filter(
        Expense.vendor_id == user_id,
        Expense.status.in_(['APPROVED', 'PENDING']),
        Expense.expense_date >= month_start
    ).scalar() or 0
    
    return jsonify({
        'expenses': expenses,
        'total': paginated.total,
        'page': page,
        'per_page': per_page,
        'pages': paginated.pages,
        'summary': {
            'total_amount': str(total_amount),
            'month_amount': str(month_amount)
        }
    }), 200


@finance_bp.route('/expenses', methods=['POST'])
@jwt_required()
@limiter.limit("30 per minute")
def create_expense():
    """Create a new expense"""
    user_id = get_jwt_identity()
    user = _require_vendor(user_id)
    if not user:
        return jsonify({'message': 'Vendor access required'}), 403
    
    data = request.get_json() or {}
    
    # Validate
    category = data.get('category')
    amount = data.get('amount')
    
    if not category:
        return jsonify({'message': 'Category is required'}), 400
    if not amount or float(amount) <= 0:
        return jsonify({'message': 'Valid amount is required'}), 400
    
    # Create expense
    expense = Expense(
        vendor_id=user_id,
        expense_date=data.get('expense_date') or utcnow(),
        category=category,
        amount=amount,
        currency=data.get('currency', 'NGN'),
        payment_method=data.get('payment_method'),
        vendor_name=data.get('vendor_name'),
        description=data.get('description'),
        receipt_url=data.get('receipt_url'),
        is_recurring=data.get('is_recurring', False),
        recurrence_frequency=data.get('recurrence_frequency'),
        tags=data.get('tags', []),
        status='PENDING'
    )
    
    db.session.add(expense)
    db.session.commit()
    
    return jsonify({
        'message': 'Expense created',
        'expense': expense.to_dict()
    }), 201


@finance_bp.route('/expenses/<int:expense_id>', methods=['PATCH'])
@jwt_required()
def update_expense(expense_id):
    """Update an expense"""
    user_id = get_jwt_identity()
    expense = Expense.query.filter_by(id=expense_id, vendor_id=user_id).first()
    
    if not expense:
        return jsonify({'message': 'Expense not found'}), 404
    
    # Block edits only if approved by a different user (formal approval workflow)
    # Self-managed vendors approve their own expenses, so we allow edits freely
    if expense.status == 'APPROVED' and expense.approved_by and expense.approved_by != int(user_id):
        return jsonify({'message': 'Cannot update an expense approved by another user'}), 400
    
    data = request.get_json() or {}
    
    # Update allowed fields
    if 'expense_date' in data:
        expense.expense_date = data['expense_date']
    if 'category' in data:
        expense.category = data['category']
    if 'amount' in data:
        expense.amount = data['amount']
    if 'payment_method' in data:
        expense.payment_method = data['payment_method']
    if 'vendor_name' in data:
        expense.vendor_name = data['vendor_name']
    if 'description' in data:
        expense.description = data['description']
    if 'receipt_url' in data:
        expense.receipt_url = data['receipt_url']
    if 'tags' in data:
        expense.tags = data['tags']
    
    expense.updated_at = utcnow()
    db.session.commit()
    
    return jsonify({
        'message': 'Expense updated',
        'expense': expense.to_dict()
    }), 200


@finance_bp.route('/expenses/<int:expense_id>', methods=['DELETE'])
@jwt_required()
def delete_expense(expense_id):
    """Delete an expense"""
    user_id = get_jwt_identity()
    expense = Expense.query.filter_by(id=expense_id, vendor_id=user_id).first()
    
    if not expense:
        return jsonify({'message': 'Expense not found'}), 404
    
    # Block deletion only if approved by a different user (formal approval workflow)
    # Self-managed vendors can delete their own expenses regardless of status
    if expense.status == 'APPROVED' and expense.approved_by and expense.approved_by != int(user_id):
        return jsonify({'message': 'Cannot delete an expense approved by another user'}), 400
    
    db.session.delete(expense)
    db.session.commit()
    
    return jsonify({'message': 'Expense deleted'}), 200


# ---------------------------------------------------------------------------
# BRANDING — Manage branding settings
# ---------------------------------------------------------------------------

@finance_bp.route('/settings/branding', methods=['GET'])
@jwt_required()
def get_branding_settings():
    """Get vendor branding settings"""
    user_id = get_jwt_identity()
    user = _require_vendor(user_id)
    if not user:
        return jsonify({'message': 'Vendor access required'}), 403
    
    settings = BrandingSettings.query.filter_by(vendor_id=user_id).first()
    
    if not settings:
        # Create default settings
        settings = BrandingSettings(vendor_id=user_id)
        db.session.add(settings)
        db.session.commit()
    
    # Fallback to Storefront values if unconfigured (Task 5)
    sf = Storefront.query.filter_by(vendor_id=user_id).first()
    if sf:
        if not settings.logo_url and sf.store_logo:
            settings.logo_url = sf.store_logo
        if not settings.business_address and sf.address:
            settings.business_address = sf.address
        if not settings.business_phone and sf.phone:
            settings.business_phone = sf.phone
        if not settings.business_email and user.email:
            settings.business_email = user.email
        if settings.primary_color == '#0b1b3b' and sf.theme_color:
            settings.primary_color = sf.theme_color
        db.session.commit()
    
    return jsonify(settings.to_dict()), 200


@finance_bp.route('/settings/branding', methods=['PATCH'])
@jwt_required()
def update_branding_settings():
    """Update vendor branding settings"""
    user_id = get_jwt_identity()
    user = _require_vendor(user_id)
    if not user:
        return jsonify({'message': 'Vendor access required'}), 403
    
    settings = BrandingSettings.query.filter_by(vendor_id=user_id).first()
    
    if not settings:
        settings = BrandingSettings(vendor_id=user_id)
        db.session.add(settings)
    
    data = request.get_json() or {}
    
    # Update fields
    if 'logo_url' in data:
        settings.logo_url = data['logo_url']
    if 'primary_color' in data:
        settings.primary_color = data['primary_color']
    if 'secondary_color' in data:
        settings.secondary_color = data['secondary_color']
    if 'accent_color' in data:
        settings.accent_color = data['accent_color']
    if 'font_family' in data:
        settings.font_family = data['font_family']
    if 'invoice_prefix' in data:
        settings.invoice_prefix = data['invoice_prefix']
    if 'invoice_template' in data:
        settings.invoice_template = data['invoice_template']
    if 'receipt_prefix' in data:
        settings.receipt_prefix = data['receipt_prefix']
    if 'receipt_template' in data:
        settings.receipt_template = data['receipt_template']
    if 'business_address' in data:
        settings.business_address = data['business_address']
    if 'business_phone' in data:
        settings.business_phone = data['business_phone']
    if 'business_email' in data:
        settings.business_email = data['business_email']
    if 'business_website' in data:
        settings.business_website = data['business_website']
    if 'tax_id' in data:
        settings.tax_id = data['tax_id']
    if 'default_payment_terms' in data:
        settings.default_payment_terms = data['default_payment_terms']
    if 'default_due_days' in data:
        settings.default_due_days = int(data['default_due_days'])
    if 'invoice_footer' in data:
        settings.invoice_footer = data['invoice_footer']
    if 'receipt_footer' in data:
        settings.receipt_footer = data['receipt_footer']
    
    settings.updated_at = utcnow()
    db.session.commit()
    
    return jsonify({
        'message': 'Branding settings updated',
        'settings': settings.to_dict()
    }), 200


# ---------------------------------------------------------------------------
# REPORTS — Financial reports
# ---------------------------------------------------------------------------

@finance_bp.route('/reports/dashboard', methods=['GET'])
@jwt_required()
def get_dashboard_overview():
    """Get financial dashboard overview"""
    user_id = get_jwt_identity()
    user = _require_vendor(user_id)
    if not user:
        return jsonify({'message': 'Vendor access required'}), 403
    
    # Date ranges
    now = datetime.now(timezone.utc)
    today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    year_start = now.replace(month=1, day=1, hour=0, minute=0, second=0, microsecond=0)
    
    # Revenue (from ledger)
    total_revenue = db.session.query(func.sum(Ledger.amount)).filter(
        Ledger.vendor_id == user_id,
        Ledger.transaction_type == 'CREDIT'
    ).scalar() or 0
    
    month_revenue = db.session.query(func.sum(Ledger.amount)).filter(
        Ledger.vendor_id == user_id,
        Ledger.transaction_type == 'CREDIT',
        Ledger.created_at >= month_start
    ).scalar() or 0
    
    # Expenses
    total_expenses = db.session.query(func.sum(Expense.amount)).filter(
        Expense.vendor_id == user_id,
        Expense.status == 'APPROVED'
    ).scalar() or 0
    
    month_expenses = db.session.query(func.sum(Expense.amount)).filter(
        Expense.vendor_id == user_id,
        Expense.status == 'APPROVED',
        Expense.expense_date >= month_start
    ).scalar() or 0
    
    # Inventory value
    inventory_value = db.session.query(
        func.sum(InventoryItem.quantity * InventoryItem.cost_price)
    ).filter(
        InventoryItem.vendor_id == user_id,
        InventoryItem.is_active == True,
        InventoryItem.cost_price.isnot(None)
    ).scalar() or 0
    
    # Orders count
    from app.models.order import Order
    total_orders = Order.query.filter_by(vendor_id=user_id).count()
    month_orders = Order.query.filter(
        Order.vendor_id == user_id,
        Order.created_at >= month_start
    ).count()
    
    # Low stock items
    low_stock_count = InventoryItem.query.filter(
        InventoryItem.vendor_id == user_id,
        InventoryItem.is_active == True,
        InventoryItem.quantity > 0,
        InventoryItem.quantity <= InventoryItem.reorder_level
    ).count()
    
    return jsonify({
        'revenue': {
            'total': str(total_revenue),
            'this_month': str(month_revenue)
        },
        'expenses': {
            'total': str(total_expenses),
            'this_month': str(month_expenses)
        },
        'profit': {
            'total': str(float(total_revenue) - float(total_expenses)),
            'this_month': str(float(month_revenue) - float(month_expenses))
        },
        'inventory': {
            'total_value': str(inventory_value),
            'low_stock_items': low_stock_count
        },
        'orders': {
            'total': total_orders,
            'this_month': month_orders
        }
    }), 200
