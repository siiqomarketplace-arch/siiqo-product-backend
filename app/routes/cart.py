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
            items.append({
                "id": item.id,
                "product_id": item.product.id,
                "name": item.product.name,
                "price": str(item.product.price),
                "quantity": item.quantity,
                "subtotal": str(subtotal),
                "image": item.product.images[0] if item.product.images else None,
                "storefront": item.product.storefront.store_name if item.product.storefront else None,
                "storefront_slug": item.product.storefront.store_slug if item.product.storefront else None,
                "stock_quantity": item.product.stock_quantity,
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

    # Group items by vendor
    vendors: dict[int, list] = {}
    for item in cart.items:
        if item.product and item.product.is_active and item.product.storefront:
            vid = item.product.storefront.vendor_id
            vendors.setdefault(vid, []).append(item)

    if not vendors:
        return jsonify({"message": "No valid items in cart"}), 400

    data = request.get_json() or {}
    referral_code = (data.get('referral_code') or '').strip().upper()
    delivery_address = data.get('delivery_address', '')

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
        total = sum(float(item.product.price) * item.quantity for item in items)
        fee_percent = 12.00
        fee_amount = total * (fee_percent / 100)

        # Create Order
        new_order = Order(
            buyer_id=user_id,
            vendor_id=vid,
            total_amount=total,
            status='PENDING',
        )
        db.session.add(new_order)
        db.session.flush()

        for item in items:
            db.session.add(OrderItem(
                order_id=new_order.id,
                product_id=item.product.id,
                price_at_purchase=item.product.price,
                quantity=item.quantity,
            ))

        # Create EscrowTransaction with all required fields
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

        # Create Invoice
        db.session.add(Invoice(
            order_id=new_order.id,
            vendor_id=vid,
            buyer_id=user_id,
        ))

        # Update CRM CustomerProfile
        profile = CustomerProfile.query.filter_by(vendor_id=vid, buyer_id=user_id).first()
        if profile:
            profile.total_spent = float(profile.total_spent or 0) + total
            profile.total_orders = (profile.total_orders or 0) + 1
            profile.last_purchase_date = _utcnow()
            # Segment upgrade
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

        # Notify vendor
        db.session.add(Notification(
            user_id=vid,
            title="New Order Received",
            message=f"You have a new order #{new_order.id} worth ₦{total:,.2f}.",
            type="ORDER",
            order_id=new_order.id,
        ))

        orders_created.append({
            "order_id": new_order.id,
            "vendor_id": vid,
            "total_amount": str(total),
            "escrow_txn": txn_number,
        })

    # Clear cart
    CartItem.query.filter_by(cart_id=cart.id).delete()
    db.session.commit()

    return jsonify({
        "message": "Checkout successful. Proceed to payment.",
        "orders": orders_created,
        "status": "success",
    }), 200
