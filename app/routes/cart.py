"""
cart.py — Cart and checkout routes
"""
import uuid
from datetime import datetime, timezone

from flask import Blueprint, request, jsonify
from flask_jwt_extended import jwt_required, get_jwt_identity

from app.extensions import db
from app.models.order import Cart, CartItem, Order, OrderItem
from app.models.product import Product
from app.models.user import User
from app.models.escrow import EscrowTransaction, EscrowStatus
from app.models.finance import Ledger, Invoice
from app.models.crm import CustomerProfile
from app.models.partnerships import Referral
from app.models.communication import Notification
from app.models.withdrawal import PODPayment
from app.utils.email import send_siiqo_email

cart_bp = Blueprint('cart', __name__)


def _utcnow():
    return datetime.now(timezone.utc)


# ---------------------------------------------------------------------------
# GET /cart/
# ---------------------------------------------------------------------------

@cart_bp.route('/', methods=['GET'])
@jwt_required()
def get_cart():
    user_id = get_jwt_identity()
    cart = Cart.query.filter_by(user_id=user_id).first()

    if not cart:
        return jsonify({"items": [], "total": "0", "item_count": 0}), 200

    items = []
    total = 0
    for item in cart.items:
        if item.product and item.product.is_active:
            subtotal = float(item.product.price) * item.quantity
            total += subtotal
            sf = item.product.storefront
            items.append({
                "id": item.id,
                "product_id": item.product.id,
                "name": item.product.name,
                "price": str(item.product.price),
                "unit_price": float(item.product.price),    # frontend reads unit_price
                "quantity": item.quantity,
                "subtotal": str(subtotal),
                "image": item.product.images[0] if item.product.images else None,
                "storefront": sf.store_name if sf else None,
                "storefront_slug": sf.store_slug if sf else None,
                "vendor_name": sf.store_name if sf else None,    # alias for cart filter
                "vendor_id": sf.vendor_id if sf else None,       # stable ID for cart filter
                "stock_quantity": item.product.stock_quantity,
                "category": item.product.category.name if item.product.category else "",
                # Negotiation fields
                "is_negotiable": item.product.is_negotiable,
                "negotiated_price": str(item.negotiated_price) if item.negotiated_price else None,
                "negotiation_id": item.negotiation_id,
                "negotiation_status": item.negotiation.status if item.negotiation else None,
                "negotiation_current_offer": str(item.negotiation.current_offer) if item.negotiation else None,
            })

    return jsonify({
        "cart_id": cart.id,
        "items": items,
        "total": str(total),
        "item_count": len(items),
    }), 200


# ---------------------------------------------------------------------------
# POST /cart/add
# ---------------------------------------------------------------------------

@cart_bp.route('/add', methods=['POST'])
@jwt_required()
def add_to_cart():
    user_id = get_jwt_identity()
    data = request.get_json() or {}
    product_id = data.get('product_id')
    quantity = int(data.get('quantity', 1))

    if not product_id:
        return jsonify({"message": "product_id is required"}), 400

    product = db.session.get(Product, product_id)
    if not product or not product.is_active:
        return jsonify({"message": "Product is not available"}), 400

    if product.stock_quantity < quantity:
        return jsonify({"message": f"Only {product.stock_quantity} in stock"}), 400

    cart = Cart.query.filter_by(user_id=user_id).first()
    if not cart:
        cart = Cart(user_id=user_id)
        db.session.add(cart)
        db.session.flush()

    existing = CartItem.query.filter_by(cart_id=cart.id, product_id=product_id).first()
    if existing:
        existing.quantity += quantity
    else:
        db.session.add(CartItem(cart_id=cart.id, product_id=product_id, quantity=quantity))

    db.session.commit()
    return jsonify({"message": "Item added to cart", "status": "success"}), 200


# ---------------------------------------------------------------------------
# PATCH /cart/update/<item_id>
# ---------------------------------------------------------------------------

@cart_bp.route('/update/<int:item_id>', methods=['PATCH'])
@jwt_required()
def update_cart_item(item_id):
    user_id = get_jwt_identity()
    data = request.get_json() or {}
    quantity = data.get('quantity')

    if quantity is None or int(quantity) < 1:
        return jsonify({"message": "Invalid quantity"}), 400

    cart = Cart.query.filter_by(user_id=user_id).first()
    if not cart:
        return jsonify({"message": "Cart not found"}), 404

    item = CartItem.query.filter_by(id=item_id, cart_id=cart.id).first()
    if not item:
        return jsonify({"message": "Item not found"}), 404

    item.quantity = int(quantity)
    db.session.commit()
    return jsonify({"message": "Cart updated", "status": "success"}), 200


# ---------------------------------------------------------------------------
# DELETE /cart/remove/<item_id>
# ---------------------------------------------------------------------------

@cart_bp.route('/remove/<int:item_id>', methods=['DELETE'])
@jwt_required()
def remove_cart_item(item_id):
    user_id = get_jwt_identity()
    cart = Cart.query.filter_by(user_id=user_id).first()
    if not cart:
        return jsonify({"message": "Cart not found"}), 404

    item = CartItem.query.filter_by(id=item_id, cart_id=cart.id).first()
    if not item:
        return jsonify({"message": "Item not found"}), 404

    db.session.delete(item)
    db.session.commit()
    return jsonify({"message": "Item removed", "status": "success"}), 200


# ---------------------------------------------------------------------------
# DELETE /cart/clear
# ---------------------------------------------------------------------------

@cart_bp.route('/clear', methods=['DELETE'])
@jwt_required()
def clear_cart():
    user_id = get_jwt_identity()
    cart = Cart.query.filter_by(user_id=user_id).first()
    if cart:
        CartItem.query.filter_by(cart_id=cart.id).delete()
        db.session.commit()
    return jsonify({"message": "Cart cleared", "status": "success"}), 200


# ---------------------------------------------------------------------------
# POST /cart/checkout  — the canonical checkout
# ---------------------------------------------------------------------------

@cart_bp.route('/checkout', methods=['POST'])
@jwt_required()
def checkout():
    user_id = get_jwt_identity()
    user = db.session.get(User, int(user_id))
    cart = Cart.query.filter_by(user_id=user_id).first()

    if not cart or not cart.items:
        return jsonify({"message": "Your cart is empty"}), 400

    data = request.get_json() or {}
    referral_code = (data.get('referral_code') or '').strip().upper()
    delivery_address = data.get('delivery_address', '')
    
    # Get payment method (ESCROW or POD)
    payment_method = (data.get('payment_method') or 'ESCROW').upper()
    if payment_method not in ['ESCROW', 'POD']:
        payment_method = 'ESCROW'  # Default to ESCROW if invalid

    # ── Optional vendor filter (storefront single-vendor checkout) ────
    # Frontend sends vendor_name or vendor_id to check out only one vendor's items.
    # If neither is provided, all cart items are checked out (marketplace flow).
    filter_vendor_name = (data.get('vendor_name') or '').strip().lower()
    filter_vendor_id = None
    try:
        raw_vid = data.get('vendor_id')
        filter_vendor_id = int(raw_vid) if raw_vid else None
    except (ValueError, TypeError):
        filter_vendor_id = None

    # Group active items by vendor, applying the optional filter
    vendors: dict[int, list] = {}
    skipped_items = []  # items with pending/countered negotiations — excluded from this checkout
    for item in cart.items:
        if not (item.product and item.product.is_active and item.product.storefront):
            continue
        sf = item.product.storefront
        vid = sf.vendor_id
        # Apply filter if provided
        if filter_vendor_id and vid != filter_vendor_id:
            continue
        if filter_vendor_name and sf.store_name.lower() != filter_vendor_name:
            continue
        # Skip items whose negotiation is still in-flight (PENDING or COUNTERED)
        if item.negotiation and item.negotiation.status in ('PENDING', 'COUNTERED'):
            skipped_items.append({
                "product_name": item.product.name,
                "reason": "Offer pending — checkout at agreed price once vendor responds",
            })
            continue
        # If accepted negotiation has expired, clear it so listed price is used
        if item.negotiation and item.negotiation.status == 'ACCEPTED':
            from app.models.negotiation import NegotiationRequest
            neg = item.negotiation
            if neg.accepted_expires_at:
                from datetime import timezone as _tz
                exp = neg.accepted_expires_at
                if exp.tzinfo is None:
                    exp = exp.replace(tzinfo=_tz.utc)
                if datetime.now(_tz.utc) > exp:
                    item.negotiated_price = None
                    item.negotiation_id = None
                    neg.status = 'EXPIRED'
        vendors.setdefault(vid, []).append(item)

    if not vendors:
        return jsonify({"message": "No valid items in cart"}), 400

    orders_created = []

    # Process referral on first-ever order
    if referral_code and Order.query.filter_by(buyer_id=user_id).count() == 0:
        referrer = User.query.filter_by(referral_code=referral_code).first()
        if referrer and referrer.id != int(user_id):
            existing_ref = Referral.query.filter_by(referred_id=user_id).first()
            if not existing_ref:
                db.session.add(Referral(
                    referrer_id=referrer.id,
                    referred_id=user_id,
                    referral_code_used=referral_code,
                    status='QUALIFIED',
                    reward_earned=1000.00,
                ))
                referrer.points_balance = float(referrer.points_balance or 0) + 1000

    for vid, items in vendors.items():
        total = sum(
            float(item.negotiated_price if item.negotiated_price else item.product.price) * item.quantity
            for item in items
        )
        fee_percent = 12.00
        fee_amount = total * (fee_percent / 100)

        # Create Order
        new_order = Order(
            buyer_id=user_id,
            vendor_id=vid,
            total_amount=total,
            status='PENDING',
            payment_method=payment_method,
        )
        db.session.add(new_order)
        db.session.flush()

        for item in items:
            effective_price = item.negotiated_price if item.negotiated_price else item.product.price
            db.session.add(OrderItem(
                order_id=new_order.id,
                product_id=item.product.id,
                price_at_purchase=effective_price,
                quantity=item.quantity,
            ))

        # Handle payment method: ESCROW or POD
        if payment_method == 'POD':
            # Pay on Delivery - Create POD payment record
            pod_payment = PODPayment(
                order_id=new_order.id,
                vendor_id=vid,
                amount=total,
                currency='NGN',
                confirmed_by_vendor=False,
            )
            db.session.add(pod_payment)
            
            # Set order status to PENDING_DELIVERY (no payment needed yet)
            new_order.status = 'PENDING_DELIVERY'
            
            # Notify vendor about POD order
            db.session.add(Notification(
                user_id=vid,
                title="New POD Order Received",
                message=f"You have a new Pay on Delivery order #{new_order.id} worth ₦{total:,.2f}. Deliver and collect payment.",
                type="ORDER",
                order_id=new_order.id,
            ))
            
            vendor = db.session.get(User, vid)
            if vendor:
                try:
                    send_siiqo_email(
                        to_email=vendor.email,
                        subject="New Pay-on-Delivery Order - Siiqo",
                        template_name="order_received_vendor",
                        first_name=vendor.first_name or "Vendor",
                        order_id=new_order.id,
                        total_amount=f"₦{total:,.2f}",
                        payment_method="Pay on Delivery"
                    )
                except Exception:
                    pass
            
            orders_created.append({
                "order_id": new_order.id,
                "vendor_id": vid,
                "total_amount": str(total),
                "payment_method": "POD",
                "status": "PENDING_DELIVERY",
            })
            
        else:  # ESCROW (default)
            # Create EscrowTransaction
            txn_number = f"ESC-{uuid.uuid4().hex[:12].upper()}"
            new_escrow = EscrowTransaction(
                order_id=new_order.id,
                transaction_number=txn_number,
                status=EscrowStatus.PENDING_PAYMENT,
                amount=total,
                fee_percent=fee_percent,
                fee_amount=fee_amount,
                currency='NGN',
            )
            db.session.add(new_escrow)
            
            # Notify vendor about escrow order
            db.session.add(Notification(
                user_id=vid,
                title="New Order Received",
                message=f"You have a new order #{new_order.id} worth ₦{total:,.2f}.",
                type="ORDER",
                order_id=new_order.id,
            ))
            
            vendor = db.session.get(User, vid)
            if vendor:
                try:
                    send_siiqo_email(
                        to_email=vendor.email,
                        subject="New Escrow Order - Siiqo",
                        template_name="order_received_vendor",
                        first_name=vendor.first_name or "Vendor",
                        order_id=new_order.id,
                        total_amount=f"₦{total:,.2f}",
                        payment_method="Escrow"
                    )
                except Exception:
                    pass
            
            orders_created.append({
                "order_id": new_order.id,
                "vendor_id": vid,
                "total_amount": str(total),
                "escrow_txn": txn_number,
                "payment_method": "ESCROW",
            })
        
        # Create Invoice (for both payment methods)
        db.session.add(Invoice(
            order_id=new_order.id,
            vendor_id=vid,
            buyer_id=user_id,
        ))

        # Update CRM CustomerProfile (for both payment methods)
        profile = CustomerProfile.query.filter_by(vendor_id=vid, buyer_id=user_id).first()
        if profile:
            profile.total_spent = float(profile.total_spent or 0) + total
            profile.total_orders = (profile.total_orders or 0) + 1
            profile.last_purchase_date = _utcnow()
            if profile.total_orders >= 10:
                profile.segment = 'VIP'
            elif profile.total_orders >= 3:
                profile.segment = 'REGULAR'
        else:
            db.session.add(CustomerProfile(
                vendor_id=vid,
                buyer_id=user_id,
                total_spent=total,
                total_orders=1,
                segment='NEW',
                last_purchase_date=_utcnow(),
            ))

    # Only clear the checked-out items (not the whole cart if vendor-filtered)
    checked_out_item_ids = [
        item.id
        for items_list in vendors.values()
        for item in items_list
    ]
    CartItem.query.filter(
        CartItem.cart_id == cart.id,
        CartItem.id.in_(checked_out_item_ids)
    ).delete(synchronize_session=False)

    db.session.commit()

    return jsonify({
        "message": "Checkout successful. Proceed to payment.",
        "orders": orders_created,
        "id": orders_created[0]["order_id"] if orders_created else None,
        "status": "success",
        "skipped_items": skipped_items,
    }), 200
