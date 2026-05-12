"""
finance.py — Finance Tools Routes
Handles: Invoices (standalone), Receipts (standalone), Customers (CRM),
         Summary, Payment links, Inventory, Expenses, Branding, Reports
"""
from flask import Blueprint, request, jsonify
from flask_jwt_extended import jwt_required, get_jwt_identity
from app.extensions import db, limiter
from app.models.finance import InventoryItem, StockMovement, Expense, BrandingSettings, Ledger
from app.models.user import User
from app.models.order import Order
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


def _get_standalone_docs(user_id: int, doc_type: str) -> list:
    """Read standalone invoices or receipts from branding settings JSON store."""
    settings = BrandingSettings.query.filter_by(vendor_id=user_id).first()
    if not settings or not settings.template_options:
        return []
    return settings.template_options.get(f'standalone_{doc_type}s', [])


def _save_standalone_doc(user_id: int, doc_type: str, doc: dict):
    """Append a standalone invoice/receipt to the JSON store."""
    settings = _get_or_create_branding(user_id)
    opts = settings.template_options or {}
    key = f'standalone_{doc_type}s'
    docs = opts.get(key, [])
    docs.insert(0, doc)  # newest first
    opts[key] = docs
    settings.template_options = opts
    db.session.commit()


def _update_standalone_doc(user_id: int, doc_type: str, doc_id: str, updates: dict) -> bool:
    """Update a field on a stored standalone doc."""
    settings = BrandingSettings.query.filter_by(vendor_id=user_id).first()
    if not settings or not settings.template_options:
        return False
    key = f'standalone_{doc_type}s'
    docs = settings.template_options.get(key, [])
    for i, d in enumerate(docs):
        if str(d.get('id')) == str(doc_id):
            docs[i] = {**d, **updates}
            settings.template_options = {**settings.template_options, key: docs}
            db.session.commit()
            return True
    return False


@finance_bp.route('/invoices', methods=['GET'])
@jwt_required()
@limiter.limit("60 per minute")
def list_invoices():
    """List all standalone invoices for the logged-in user"""
    user_id = get_jwt_identity()
    status_filter = request.args.get('status')
    docs = _get_standalone_docs(user_id, 'invoice')
    if status_filter:
        docs = [d for d in docs if d.get('status') == status_filter]
    return jsonify({
        'status': 'success',
        'invoices': docs,
        'total': len(docs)
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

    invoice = {
        'id': str(uuid.uuid4()),
        'invoice_number': invoice_number,
        'customer_name': customer_name,
        'customer_email': data.get('customer_email', ''),
        'customer_phone': data.get('customer_phone', ''),
        'customer_address': data.get('customer_address', ''),
        'line_items': line_items,
        'subtotal': subtotal,
        'discount': discount,
        'tax_rate': tax_rate,
        'tax_amount': tax_amount,
        'total': total,
        'amount': total,
        'currency': data.get('currency', 'NGN'),
        'notes': data.get('notes', ''),
        'due_date': data.get('due_date', ''),
        'status': 'draft',
        'payment_link_token': str(uuid.uuid4()),
        'created_at': utcnow().isoformat(),
        'updated_at': utcnow().isoformat(),
    }

    _save_standalone_doc(user_id, 'invoice', invoice)

    return jsonify({
        'status': 'success',
        'message': 'Invoice created successfully',
        'invoice': invoice
    }), 201


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

    updates = {
        'status': new_status,
        'updated_at': utcnow().isoformat(),
    }
    if new_status == 'paid' and data.get('payment_method'):
        updates['payment_method'] = data['payment_method']

    found = _update_standalone_doc(user_id, 'invoice', invoice_id, updates)
    if not found:
        return jsonify({'message': 'Invoice not found'}), 404

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
    docs = _get_standalone_docs(user_id, 'receipt')
    return jsonify({
        'status': 'success',
        'receipts': docs,
        'total': len(docs)
    }), 200


@finance_bp.route('/receipts', methods=['POST'])
@jwt_required()
@limiter.limit("30 per minute")
def create_receipt():
    """Create a new standalone receipt"""
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

    receipt = {
        'id': str(uuid.uuid4()),
        'receipt_number': receipt_number,
        'customer_name': customer_name,
        'customer_email': data.get('customer_email', ''),
        'customer_phone': data.get('customer_phone', ''),
        'line_items': line_items,
        'subtotal': subtotal,
        'tax_amount': tax_amount,
        'discount': discount,
        'total': total,
        'amount': total,
        'currency': data.get('currency', 'NGN'),
        'payment_method': data.get('payment_method', 'Cash'),
        'notes': data.get('notes', ''),
        'status': 'paid',
        'created_at': utcnow().isoformat(),
        'updated_at': utcnow().isoformat(),
    }

    _save_standalone_doc(user_id, 'receipt', receipt)

    return jsonify({
        'status': 'success',
        'message': 'Receipt created successfully',
        'receipt': receipt
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

    # Standalone invoices & receipts
    invoices = _get_standalone_docs(user_id, 'invoice')
    receipts = _get_standalone_docs(user_id, 'receipt')

    paid_invoices = [i for i in invoices if i.get('status') == 'paid']
    pending_invoices = [i for i in invoices if i.get('status') in ('draft', 'sent')]
    overdue_invoices = [i for i in invoices if i.get('status') == 'overdue']

    total_invoiced = sum(float(i.get('total', 0)) for i in invoices)
    total_paid = sum(float(i.get('total', 0)) for i in paid_invoices)
    total_pending = sum(float(i.get('total', 0)) for i in pending_invoices)
    total_receipt_revenue = sum(float(r.get('total', 0)) for r in receipts)

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
    # Search all vendors' invoices for this token
    # Since we store in BrandingSettings JSON, we do a simple scan
    all_settings = BrandingSettings.query.all()
    for settings in all_settings:
        if not settings.template_options:
            continue
        for inv in settings.template_options.get('standalone_invoices', []):
            if inv.get('payment_link_token') == token:
                return jsonify({
                    'status': 'success',
                    'invoice': inv
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
            'usage': {'current_month_invoices': 0, 'limit': 5}
        }), 200

    # Count this month's standalone invoices + receipts
    settings = BrandingSettings.query.filter_by(vendor_id=user_id).first()
    month_count = 0
    if settings and settings.template_options:
        now = utcnow()
        month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        for doc_type in ('standalone_invoices', 'standalone_receipts'):
            for doc in settings.template_options.get(doc_type, []):
                try:
                    created = datetime.fromisoformat(doc.get('created_at', ''))
                    if created.replace(tzinfo=timezone.utc) >= month_start:
                        month_count += 1
                except Exception:
                    pass

    return jsonify({
        'status': 'success',
        'has_active_subscription': False,  # TODO: wire to subscription model
        'plan_type': 'FREE',
        'usage': {
            'current_month_invoices': month_count,
            'limit': 5
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
        Expense.status == 'APPROVED'
    ).scalar() or 0
    
    # This month
    now = datetime.now(timezone.utc)
    month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    month_amount = db.session.query(
        func.sum(Expense.amount)
    ).filter(
        Expense.vendor_id == user_id,
        Expense.status == 'APPROVED',
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
    
    # Can't update approved expenses
    if expense.status == 'APPROVED':
        return jsonify({'message': 'Cannot update approved expense'}), 400
    
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
    
    # Can't delete approved expenses
    if expense.status == 'APPROVED':
        return jsonify({'message': 'Cannot delete approved expense'}), 400
    
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
