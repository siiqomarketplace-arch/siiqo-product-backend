"""
bridge.py — Route aliases & missing endpoints

Bridges the gap between frontend API call paths and the actual backend
route structure. All routes here either alias existing logic or implement
the missing endpoints the frontend expects at /api/*.

This file is intentionally kept thin — business logic lives in the
dedicated route files. Bridge routes delegate or re-use that logic.
"""
import logging
import uuid
import os
import hmac
import hashlib
import requests as http_requests
from datetime import datetime, timezone

from flask import Blueprint, request, jsonify
from flask_jwt_extended import jwt_required, get_jwt_identity, create_access_token

from app.extensions import db
from app.models.user import User, Storefront, UserRole
from app.models.product import Product, Category, Catalog
from app.models.order import Order, OrderItem, Cart, CartItem
from app.models.escrow import EscrowTransaction, EscrowStatus
from app.models.admin import Favorite, VendorSubscription, SubscriptionPlan
from app.models.partnerships import PartnerApplication, Referral
from app.models.community import Review
from app.models.communication import Notification
from app.models.finance import Ledger
from app.utils.upload import save_uploaded_file

bridge_bp = Blueprint('bridge', __name__)


def _utcnow():
    return datetime.now(timezone.utc)


# ===========================================================================
# PRODUCT ROUTES  (frontend calls /api/products/*)
# Delegates to vendor route logic
# ===========================================================================

@bridge_bp.route('/products/my-products', methods=['GET'])
@jwt_required()
def my_products():
    from app.routes.vendor import my_products as _my_products
    return _my_products()


@bridge_bp.route('/products/add', methods=['POST'])
@jwt_required()
def add_product():
    from app.routes.vendor import add_product as _add_product
    return _add_product()


@bridge_bp.route('/products/update/<int:product_id>', methods=['PATCH', 'PUT'])
@jwt_required()
def update_product(product_id):
    from app.routes.vendor import edit_product as _edit_product
    return _edit_product(product_id)


@bridge_bp.route('/products/delete/<int:product_id>', methods=['DELETE'])
@jwt_required()
def delete_product(product_id):
    from app.routes.vendor import delete_product as _delete_product
    return _delete_product(product_id)


@bridge_bp.route('/products/categories', methods=['GET'])
def get_categories():
    from app.routes.public import get_categories as _get_categories
    return _get_categories()


@bridge_bp.route('/products/category', methods=['POST'])
@jwt_required()
def create_category():
    data = request.get_json() or {}
    import re
    name = (data.get('name') or '').strip()
    if not name:
        return jsonify({"message": "Name required"}), 400
    slug = re.sub(r'[^a-z0-9]+', '-', name.lower()).strip('-')
    cat = Category(name=name, slug=slug)
    db.session.add(cat)
    db.session.commit()
    return jsonify({"message": "Category created", "id": cat.id, "status": "success"}), 201

@bridge_bp.route('/products/catalogs', methods=['GET'])
@jwt_required()
def get_catalogs():
    user_id = get_jwt_identity()
    user = db.session.get(User, int(user_id))
    if not user or not user.storefront:
        return jsonify({"status": "success", "catalogs": []}), 200

    catalogs = Catalog.query.filter_by(vendor_id=user.id).all()

    result = []
    for cat in catalogs:
        products = Product.query.filter_by(
            catalog_id=cat.id, storefront_id=user.storefront.id, is_active=True
        ).all()
        result.append({
            "id": cat.id,
            "name": cat.name,
            "description": cat.description or "",
            "image": None,  # image upload not yet supported for catalogs
            "products": [{
                "id": p.id,
                "name": p.name,
                "price": str(p.price),
                "images": p.images or [],
                "description": p.description,
                "category": p.category.name if p.category else "",
                "condition": p.condition,
                "location": p.location,
                "latitude": p.latitude,
                "longitude": p.longitude,
                "is_negotiable": p.is_negotiable,
                "floor_price": str(p.floor_price) if p.floor_price else None,
                "status": "active" if p.is_active else "inactive",
                "quantity": p.stock_quantity,
            } for p in products],
        })

    return jsonify({"status": "success", "catalogs": result}), 200


@bridge_bp.route('/products/catalogs', methods=['POST'])
@jwt_required()
def create_catalog():
    user_id = get_jwt_identity()
    user = db.session.get(User, int(user_id))
    if not user or not user.storefront:
        return jsonify({"message": "Vendor storefront required"}), 403

    # Accept both JSON, URLSearchParams (form-urlencoded), and multipart
    if request.is_json:
        data = request.get_json() or {}
    else:
        data = request.form.to_dict()

    name = (data.get("name") or "").strip()
    if not name:
        return jsonify({"message": "Catalog name is required"}), 400

    description = (data.get("description") or "").strip()
    product_ids_raw = data.get("product_ids", "")
    product_ids = [int(x.strip()) for x in product_ids_raw.split(",") if x.strip().isdigit()]

    cat = Catalog(vendor_id=user.id, name=name, description=description)
    db.session.add(cat)
    db.session.flush()  # get cat.id before commit

    # Link products to this catalog
    if product_ids:
        Product.query.filter(
            Product.id.in_(product_ids),
            Product.storefront_id == user.storefront.id,
        ).update({"catalog_id": cat.id}, synchronize_session=False)

    db.session.commit()
    return jsonify({"status": "success", "message": "Catalog created", "id": cat.id, "catalog": {"id": cat.id, "name": cat.name}}), 201


@bridge_bp.route('/products/catalogs/<int:catalog_id>', methods=['PATCH'])
@jwt_required()
def update_catalog(catalog_id):
    """Rename a catalog and/or update its product assignments."""
    user_id = get_jwt_identity()
    user = db.session.get(User, int(user_id))
    if not user or not user.storefront:
        return jsonify({"message": "Vendor storefront required"}), 403

    cat = db.session.get(Catalog, catalog_id)
    if not cat or cat.vendor_id != user.id:
        return jsonify({"message": "Catalog not found"}), 404

    # Accept both JSON, URLSearchParams (form-urlencoded), and multipart
    if request.is_json:
        data = request.get_json() or {}
    else:
        data = request.form.to_dict()

    if data.get("name"):
        cat.name = data["name"].strip()
    if "description" in data:
        cat.description = data["description"].strip()

    # Re-assign products if product_ids provided
    product_ids_raw = data.get("product_ids", "")
    if product_ids_raw:
        product_ids = [int(x.strip()) for x in product_ids_raw.split(",") if x.strip().isdigit()]
        # First clear old assignments for this catalog
        Product.query.filter_by(
            catalog_id=cat.id, storefront_id=user.storefront.id
        ).update({"catalog_id": None}, synchronize_session=False)
        # Then assign new ones
        if product_ids:
            Product.query.filter(
                Product.id.in_(product_ids),
                Product.storefront_id == user.storefront.id,
            ).update({"catalog_id": cat.id}, synchronize_session=False)

    db.session.commit()
    return jsonify({"status": "success", "message": "Catalog updated", "id": cat.id}), 200


@bridge_bp.route('/products/catalogs/<int:catalog_id>', methods=['DELETE'])
@jwt_required()
def delete_catalog(catalog_id):
    """Delete a catalog. Products inside become uncategorized (catalog_id set to null)."""
    user_id = get_jwt_identity()
    user = db.session.get(User, int(user_id))
    if not user or not user.storefront:
        return jsonify({"message": "Vendor storefront required"}), 403

    cat = db.session.get(Catalog, catalog_id)
    if not cat or cat.vendor_id != user.id:
        return jsonify({"message": "Catalog not found"}), 404

    # Unlink products — they remain in the store but become ungrouped
    Product.query.filter_by(catalog_id=cat.id).update({"catalog_id": None}, synchronize_session=False)
    db.session.delete(cat)
    db.session.commit()
    return jsonify({"status": "success", "message": f"Catalog '{cat.name}' deleted. Products are now ungrouped."}), 200


# ===========================================================================
# VENDOR ORDERS  (frontend calls /api/vendor-orders/*)
# ===========================================================================

@bridge_bp.route('/vendor-orders/orders', methods=['GET'])
@jwt_required()
def get_vendor_orders():
    from app.routes.vendor import get_orders as _get_orders
    return _get_orders()


@bridge_bp.route('/vendor-orders/orders/<int:order_id>/confirm-payment', methods=['PATCH', 'POST'])
@jwt_required()
def confirm_payment(order_id):
    user_id = get_jwt_identity()
    user = db.session.get(User, int(user_id))
    if not user:
        return jsonify({"message": "Unauthorized"}), 403

    order = db.session.get(Order, order_id)
    if not order or order.vendor_id != user.id:
        return jsonify({"message": "Not found or unauthorized"}), 404

    order.status = 'PROCESSING'
    db.session.commit()
    return jsonify({"message": "Payment confirmed", "status": "success"}), 200


@bridge_bp.route('/vendor-orders/orders/<int:order_id>/status', methods=['PUT', 'PATCH'])
@jwt_required()
def update_order_status_bridge(order_id):
    from app.routes.vendor import update_order_status as _update_order_status
    return _update_order_status(order_id)


@bridge_bp.route('/vendor-orders/analytics/revenue', methods=['GET'])
@jwt_required()
def revenue_analytics():
    user_id = get_jwt_identity()
    user = db.session.get(User, int(user_id))
    if not user:
        return jsonify({"message": "Unauthorized"}), 403

    orders = Order.query.filter_by(vendor_id=user.id, status='COMPLETED').all()
    total = sum(float(o.total_amount) for o in orders)
    monthly: dict = {}
    for o in orders:
        if o.created_at:
            key = o.created_at.strftime('%Y-%m')
            monthly[key] = monthly.get(key, 0) + float(o.total_amount)

    return jsonify({
        "status": "success",
        "total_revenue": total,
        "monthly_breakdown": [{"month": k, "revenue": v} for k, v in sorted(monthly.items())],
        "total_orders": len(orders),
    }), 200


@bridge_bp.route('/vendor/balance', methods=['GET'])
@jwt_required()
def vendor_balance():
    user_id = get_jwt_identity()
    user = db.session.get(User, int(user_id))
    if not user:
        return jsonify({"message": "Unauthorized"}), 403

    from sqlalchemy import func
    from app.models.escrow import EscrowTransaction, EscrowStatus

    credits = db.session.query(func.sum(Ledger.amount)).filter_by(
        vendor_id=user.id, transaction_type='CREDIT'
    ).scalar() or 0
    debits = db.session.query(func.sum(Ledger.amount)).filter_by(
        vendor_id=user.id, transaction_type='DEBIT'
    ).scalar() or 0
    net = float(credits) - float(debits)

    # Locked balance = sum of escrow transactions in IN_ESCROW or SHIPPED status
    locked = db.session.query(func.sum(EscrowTransaction.amount)).join(
        Order, EscrowTransaction.order_id == Order.id
    ).filter(
        Order.vendor_id == user.id,
        EscrowTransaction.status.in_([EscrowStatus.IN_ESCROW, EscrowStatus.SHIPPED, EscrowStatus.DELIVERED])
    ).scalar() or 0
    locked = float(locked)

    # Available = net ledger balance (released funds not yet withdrawn)
    available = max(0.0, net)
    payout_threshold = 5000.0
    can_withdraw = available >= payout_threshold

    return jsonify({
        "status": "success",
        "gross_revenue": float(credits),
        "net_balance": net,
        "total_debits": float(debits),
        # Fields expected by /vendor/escrow page
        "locked_balance": locked,
        "available_balance": available,
        "payout_threshold": payout_threshold,
        "can_withdraw": can_withdraw,
        "siiqo_fee_percent": 12,
        "currency": "NGN",
    }), 200


# ===========================================================================
# BUYER ROUTES  (frontend calls /api/buyers/*)
# ===========================================================================

@bridge_bp.route('/buyers/storefronts', methods=['GET'])
def get_buyer_storefronts():
    from app.routes.public import get_storefronts as _get_storefronts
    return _get_storefronts()


@bridge_bp.route('/buyers/favorites', methods=['GET'])
@jwt_required()
def get_favorites():
    user_id = get_jwt_identity()
    favs = Favorite.query.filter_by(user_id=user_id).all()
    items = []
    for f in favs:
        if f.product_id:
            p = db.session.get(Product, f.product_id)
            if p and p.is_active:
                items.append({
                    "fav_id": f.id,
                    "type": "product",
                    "id": p.id,
                    "name": p.name,
                    "price": str(p.price),
                    "images": p.images or [],
                    "storefront": p.storefront.store_name if p.storefront else None,
                    "storefront_slug": p.storefront.store_slug if p.storefront else None,
                })
        elif f.storefront_id:
            s = db.session.get(Storefront, f.storefront_id)
            if s and s.is_live:
                items.append({
                    "fav_id": f.id,
                    "type": "storefront",
                    "id": s.id,
                    "name": s.store_name,
                    "store_slug": s.store_slug,
                    "logo": s.store_logo,
                })
    return jsonify({"status": "success", "favorites": items}), 200


@bridge_bp.route('/buyers/favorites/<int:product_id>', methods=['POST'])
@jwt_required()
def toggle_favorite(product_id):
    user_id = get_jwt_identity()
    existing = Favorite.query.filter_by(user_id=user_id, product_id=product_id).first()
    if existing:
        db.session.delete(existing)
        db.session.commit()
        return jsonify({"message": "Removed from favorites", "action": "removed"}), 200

    db.session.add(Favorite(user_id=user_id, product_id=product_id))
    db.session.commit()
    return jsonify({"message": "Added to favorites", "action": "added"}), 201


# ===========================================================================
# BUYER ORDERS  (frontend calls /api/buyer-orders/*)
# ===========================================================================

@bridge_bp.route('/buyer-orders/checkout', methods=['POST'])
@jwt_required()
def buyer_checkout():
    """Delegates to /api/cart/checkout"""
    from app.routes.cart import checkout as _checkout
    return _checkout()


@bridge_bp.route('/buyer-orders/history', methods=['GET'])
@jwt_required()
def buyer_order_history():
    user_id = get_jwt_identity()
    from app.models.order import OrderItem
    from sqlalchemy.orm import joinedload
    orders = (
        Order.query
        .filter_by(buyer_id=int(user_id))
        .order_by(Order.created_at.desc())
        .options(
            joinedload(Order.items).joinedload(OrderItem.product),
            joinedload(Order.buyer),
        )
        .limit(500)
        .all()
    )

    # ── Live payment sync for stuck PENDING orders ──────────────────────────
    # If an order is PENDING and has an escrow record, the webhook may have
    # been missed. Verify directly with the active provider.
    active_provider = os.environ.get("ACTIVE_ESCROW_PROVIDER", "payscrow").lower()

    for o in orders:
        if o.status == 'PENDING':
            escrow_check = EscrowTransaction.query.filter_by(order_id=o.id).first()
            if escrow_check and escrow_check.transaction_number:
                try:
                    if active_provider == "paystack":
                        from app.services.escrow.paystack_provider import PaystackProvider
                        verify = PaystackProvider().verify_transaction(
                            escrow_check.transaction_number
                        )
                        if verify.get("success"):
                            escrow_check.status = EscrowStatus.IN_ESCROW
                            escrow_check.paid_at = escrow_check.paid_at or datetime.now(timezone.utc)
                            o.status = 'PAID'
                            db.session.commit()
                    else:
                        # Legacy Payscrow sync
                        from app.routes.escrow import _payscrow_env as _ps_env
                        import requests as _http
                        ps_key, ps_base = _ps_env()
                        if ps_key:
                            r = _http.get(
                                f"{ps_base}/api/v3/marketplace/transactions/"
                                f"{escrow_check.transaction_number}/status",
                                headers={"BrokerApiKey": ps_key},
                                timeout=8,
                            )
                            if r.status_code == 200:
                                ps_status = str(r.json().get('paymentStatus', '')).lower()
                                if ps_status in ['paid', 'completed', 'pendingsettlement']:
                                    escrow_check.status = EscrowStatus.IN_ESCROW
                                    escrow_check.paid_at = (
                                        escrow_check.paid_at or datetime.now(timezone.utc)
                                    )
                                    if r.json().get('escrowCode'):
                                        escrow_check.escrow_code = r.json().get('escrowCode')
                                    o.status = 'PAID'
                                    db.session.commit()
                except Exception:
                    pass  # non-fatal — show whatever status we have

    result = []
    for o in orders:
        vendor = db.session.get(User, o.vendor_id)
        vendor_store = vendor.storefront if vendor else None
        escrow = EscrowTransaction.query.filter_by(order_id=o.id).first()

        # Build items list so the buyer UI can show product names & quantities
        items = [{
            "name": (item.product.name if item.product else "Product"),
            "product_name": (item.product.name if item.product else "Product"),
            "qty": item.quantity,
            "quantity": item.quantity,
            "price": float(item.price_at_purchase),
            "unit_price": float(item.price_at_purchase),
            "image": (item.product.images[0] if item.product and item.product.images else None),
            "product_type": (item.product.product_type if item.product else "physical") or "physical",
            "file_url": (item.product.file_url if item.product else None),
            "booking_link": (item.product.booking_link if item.product else None),
        } for item in (o.items or [])]

        result.append({
            "id": o.id,
            "order_id": o.id,
            "total": float(o.total_amount),
            "total_amount": float(o.total_amount),
            "status": o.status,
            # Always return uppercase so frontend comparison works cleanly
            "payment_method": (o.payment_method or "ESCROW").upper(),
            # ISO string for normalization + human-friendly date
            "created_at": o.created_at.isoformat() if o.created_at else None,
            "date": o.created_at.strftime('%d %b %Y') if o.created_at else "",
            "vendor": (
                vendor_store.store_name if vendor_store
                else (vendor.full_name if vendor else "Unknown Vendor")
            ),
            "vendor_name": (
                vendor_store.store_name if vendor_store
                else (vendor.full_name if vendor else "Unknown Vendor")
            ),
            "store_name": (vendor_store.store_name if vendor_store else None),
            "vendor_id": o.vendor_id,
            "items": items,
            # Escrow fields
            "escrow_status": escrow.status if escrow else None,
            "transaction_number": escrow.transaction_number if escrow else None,
            "delivery_otp": escrow.escrow_code if escrow else None,
            "logistics": o.logistics_provider_id if hasattr(o, 'logistics_provider_id') else None,
            "city": o.delivery_city if hasattr(o, 'delivery_city') else None,
        })

    return jsonify({"status": "success", "orders": result}), 200


@bridge_bp.route('/buyer-orders/<int:order_id>', methods=['GET'])
@jwt_required()
def get_buyer_order_detail(order_id):
    user_id = get_jwt_identity()
    order = db.session.get(Order, order_id)
    if not order:
        return jsonify({"message": "Order not found"}), 404
    if order.buyer_id != int(user_id):
        return jsonify({"message": "Unauthorized"}), 403

    items = [{
        "product_name": item.product.name if item.product else "Unknown",
        "quantity": item.quantity,
        "unit_price": float(item.price_at_purchase),
        "image": (item.product.images[0] if item.product and item.product.images else None),
        "product_type": (item.product.product_type if item.product else "physical") or "physical",
        "file_url": (item.product.file_url if item.product else None),
        "booking_link": (item.product.booking_link if item.product else None),
    } for item in (order.items or [])]

    vendor = db.session.get(User, order.vendor_id)
    vendor_store = vendor.storefront if vendor else None
    buyer = db.session.get(User, int(user_id))

    escrow = EscrowTransaction.query.filter_by(order_id=order.id).first()

    from app.models.escrow import LogisticsAssignment
    assignment = LogisticsAssignment.query.filter_by(order_id=order.id).first()

    return jsonify({
        "status": "success",
        "order": {
            "id": order.id,
            "status": order.status,
            "total_amount": float(order.total_amount),
            "tracking_number": order.tracking_number,
            "items": items,
            "buyer_name": buyer.full_name if buyer else "",
            "buyer_phone": buyer.phone if buyer else "",
            "delivery_address": (
                f"{order.delivery_address}, {order.delivery_city or ''}, {order.delivery_state or ''}".strip(', ')
                if order.delivery_address
                else (buyer.address if buyer and hasattr(buyer, 'address') else None)
            ),
            "vendor_id": order.vendor_id,
            "vendor_name": vendor_store.store_name if vendor_store else "Unknown Vendor",
            "vendor_address": (
                f"{vendor_store.address}, {vendor_store.city or ''}, {vendor_store.state or ''}".strip(', ')
                if vendor_store and vendor_store.address
                else ((vendor_store.city or "Lagos") if vendor_store else "Lagos")
            ),
            "escrow": escrow.to_dict() if escrow else None,
            # Convenience alias so both buyer order pages get the OTP directly
            "delivery_otp": escrow.escrow_code if escrow else None,
            "logistics_assignment_id": assignment.id if assignment else None,
        },
    }), 200


@bridge_bp.route('/buyer-orders/<int:order_id>/escrow-code', methods=['GET'])
@jwt_required()
def get_escrow_code(order_id):
    escrow = EscrowTransaction.query.filter_by(order_id=order_id).first()
    if not escrow:
        return jsonify({"message": "No escrow transaction found"}), 404
    return jsonify(escrow.to_dict()), 200


@bridge_bp.route('/buyer-orders/confirm-received', methods=['POST'])
@jwt_required()
def confirm_received():
    """Buyer confirms delivery → delegates to escrow release."""
    from app.routes.escrow import release_escrow as _release
    return _release()


@bridge_bp.route('/buyer-orders/raise-dispute', methods=['POST'])
@jwt_required()
def raise_dispute_alias():
    """Alias: buyer raises dispute → delegates to escrow dispute."""
    from app.routes.escrow import raise_dispute as _dispute
    return _dispute()


# ===========================================================================
# REVIEWS
# ===========================================================================

@bridge_bp.route('/reviews', methods=['POST'])
@jwt_required()
def submit_review():
    user_id = get_jwt_identity()
    data = request.get_json() or {}
    order_id = data.get('order_id')
    # Accept both field names the frontend sends
    review_text = (data.get('review') or data.get('review_text') or '').strip()

    # Validate vendor_rating is an integer 1–5
    try:
        vendor_rating = int(data.get('vendor_rating', 0))
    except (ValueError, TypeError):
        vendor_rating = 0
    if vendor_rating < 1 or vendor_rating > 5:
        return jsonify({"message": "vendor_rating must be between 1 and 5"}), 400

    # product_rating is optional but must also be 1–5 if provided
    product_rating = data.get('product_rating')
    if product_rating is not None:
        try:
            product_rating = int(product_rating)
            if product_rating < 1 or product_rating > 5:
                product_rating = None
        except (ValueError, TypeError):
            product_rating = None

    if not order_id:
        return jsonify({"message": "order_id is required"}), 400

    order = db.session.get(Order, order_id)
    if not order or order.buyer_id != int(user_id):
        return jsonify({"message": "Order not found or unauthorized"}), 404

    # Order status gate: Order must be completed/delivered, or escrow released/refunded
    order_status = (order.status or "").upper()
    escrow = EscrowTransaction.query.filter_by(order_id=order_id).first()
    escrow_status = (escrow.status or "").upper() if escrow else ""
    if order_status not in ['COMPLETED', 'DELIVERED'] and escrow_status not in ['RELEASED', 'REFUNDED']:
        return jsonify({"message": "You can only review orders that have been delivered or completed."}), 400

    # Prevent duplicate reviews
    existing = Review.query.filter_by(order_id=order_id, buyer_id=user_id).first()
    if existing:
        return jsonify({"message": "You have already reviewed this order"}), 409

    review = Review(
        order_id=order_id,
        buyer_id=user_id,
        vendor_id=order.vendor_id,
        vendor_rating=vendor_rating,
        product_rating=product_rating,
        review_text=review_text,
    )
    db.session.add(review)

    # Notify vendor
    db.session.add(Notification(
        user_id=order.vendor_id,
        title=f"New Review ({vendor_rating}/5 ⭐)",
        message=review_text or f"A buyer left a {vendor_rating}-star review for Order #{order_id}.",
        type="REVIEW",
        order_id=order_id,
    ))

    db.session.commit()

    # Trigger trust score recalculation instantly
    try:
        from app.services.trust import recalculate_vendor_trust
        recalculate_vendor_trust(order.vendor_id, reason="Review Submitted")
    except Exception as e:
        logging.error(f"[TRUST ERROR] Failed to recalculate trust on review submit: {e}")

    return jsonify({"message": "Review submitted. Thank you!", "status": "success"}), 201


# ===========================================================================
# AUTH ALIASES
# ===========================================================================

@bridge_bp.route('/user/profile', methods=['GET'])
@jwt_required()
def user_profile_alias():
    from app.routes.auth import profile as _profile
    return _profile()


@bridge_bp.route('/user/update-profile', methods=['PUT', 'PATCH'])
@jwt_required()
def update_user_profile():
    """Update buyer personal info (fullname, email, phone)."""
    user_id = get_jwt_identity()
    user = db.session.get(User, int(user_id))
    if not user:
        return jsonify({"message": "User not found"}), 404

    # Accept both JSON and FormData
    if request.is_json:
        data = request.get_json() or {}
        # Support flat keys or nested personal_info object
        pi = data.get('personal_info', data)
        fullname = (pi.get('fullname') or '').strip()
        email    = (pi.get('email')    or '').strip().lower()
        phone    = (pi.get('phone')    or '').strip()
    else:
        # FormData with bracket notation: personal_info[fullname]
        fullname = (request.form.get('personal_info[fullname]') or request.form.get('fullname') or '').strip()
        email    = (request.form.get('personal_info[email]')    or request.form.get('email')    or '').strip().lower()
        phone    = (request.form.get('personal_info[phone]')    or request.form.get('phone')    or '').strip()

    if fullname:
        parts = fullname.split(' ', 1)
        user.first_name = parts[0]
        user.last_name  = parts[1] if len(parts) > 1 else (user.last_name or '')
    if email and email != user.email:
        if User.query.filter(User.email == email, User.id != user.id).first():
            return jsonify({"message": "That email is already in use by another account"}), 409
        user.email = email
    if phone:
        user.phone = phone

    db.session.commit()

    from app.routes.auth import _user_payload
    return jsonify({
        "message": "Profile updated successfully",
        "status": "success",
        "user": _user_payload(user),
    }), 200


@bridge_bp.route('/auth/change-password', methods=['POST'])
@jwt_required()
def change_password():
    """Change password for authenticated user."""
    user_id = get_jwt_identity()
    user = db.session.get(User, int(user_id))
    if not user:
        return jsonify({"message": "User not found"}), 404

    data = request.get_json() or {}
    current_password = data.get('current_password', '')
    new_password     = data.get('new_password', '')

    if not current_password or not new_password:
        return jsonify({"message": "current_password and new_password are required"}), 400

    if not user.check_password(current_password):
        return jsonify({"message": "Current password is incorrect"}), 403

    if len(new_password) < 8:
        return jsonify({"message": "New password must be at least 8 characters"}), 400

    user.set_password(new_password)
    db.session.commit()
    return jsonify({"message": "Password changed successfully", "status": "success"}), 200


@bridge_bp.route('/upload-profile-pic', methods=['POST'])
@jwt_required()
def upload_profile_pic_alias():
    from app.routes.auth import upload_profile_pic as _upload
    return _upload()


@bridge_bp.route('/buyer-orders/upload-proof/<int:order_id>', methods=['POST'])
@jwt_required()
def upload_payment_proof(order_id):
    """
    Upload a payment proof image for an order.
    Used by the frontend when buyers need to submit manual payment evidence.
    Stores the image URL on the order's escrow transaction notes field.
    """
    user_id = get_jwt_identity()
    from app.models.order import Order
    from app.models.escrow import EscrowTransaction

    order = db.session.get(Order, order_id)
    if not order:
        return jsonify({"message": "Order not found"}), 404
    if order.buyer_id != int(user_id):
        return jsonify({"message": "Unauthorized"}), 403

    proof_file = request.files.get('proof') or request.files.get('file') or request.files.get('image')
    if not proof_file:
        return jsonify({"message": "No file uploaded. Use field name 'proof', 'file', or 'image'."}), 400

    try:
        proof_url = save_uploaded_file(proof_file, subfolder='payment_proofs')
    except ValueError as e:
        return jsonify({"message": str(e)}), 400

    # Attach to escrow transaction as a note
    escrow = EscrowTransaction.query.filter_by(order_id=order_id).first()
    if escrow:
        existing = escrow.dispute_reason or ''
        escrow.dispute_reason = f"[PAYMENT_PROOF: {proof_url}] {existing}".strip()

    # Also add a notification for the vendor
    db.session.add(Notification(
        user_id=order.vendor_id,
        title="Payment Proof Submitted",
        message=f"Buyer submitted payment proof for Order #{order_id}.",
        type="ORDER",
        order_id=order_id,
    ))
    db.session.commit()

    return jsonify({
        "status": "success",
        "message": "Payment proof uploaded successfully.",
        "proof_url": proof_url,
    }), 200


# ===========================================================================
# PARTNER LOGIN  (PartnerAuthContext calls /auth/partner/login)
# ===========================================================================

@bridge_bp.route('/auth/partner/login', methods=['POST'])
def partner_login():
    data = request.get_json() or {}
    email = (data.get('email') or '').strip().lower()
    password = data.get('password', '')

    user = User.query.filter_by(email=email).first()
    if not user or not user.check_password(password):
        return jsonify({"message": "Invalid credentials"}), 401

    if not user.is_active:
        return jsonify({"message": "Account suspended"}), 403

    if user.role not in [UserRole.PARTNER, UserRole.ADMIN]:
        return jsonify({
            "message": "This account does not have partner access. Apply at siiqo.com/partners/apply"
        }), 403

    access_token = create_access_token(identity=str(user.id))
    app_record = PartnerApplication.query.filter_by(user_id=user.id, status='APPROVED').first()

    # Calculate real lifetime earnings from completed deliveries
    from sqlalchemy import func
    from app.models.escrow import LogisticsAssignment
    total_earned = db.session.query(func.sum(LogisticsAssignment.delivery_fee)).filter_by(
        partner_id=user.id, status='DELIVERED'
    ).scalar() or 0.0

    return jsonify({
        "access_token": access_token,
        "partner": {
            "id": user.id,
            "email": user.email,
            "name": user.full_name,
            "partner_role": app_record.service_type if app_record else "LOGISTICS",
            "status": "ACTIVE",
            "wallet_balance": float(total_earned),
        },
    }), 200


# ===========================================================================
# PAYMENTS
# ===========================================================================

PAYSTACK_SECRET_KEY = os.environ.get('PAYSTACK_SECRET_KEY', '')
PAYSTACK_BASE_URL = 'https://api.paystack.co'
PAYSTACK_MONTHLY_PLAN = os.environ.get('PAYSTACK_MONTHLY_PLAN_CODE', '')
PAYSTACK_ANNUAL_PLAN = os.environ.get('PAYSTACK_ANNUAL_PLAN_CODE', '')
SITE_URL = os.environ.get('SITE_URL', os.environ.get('NEXT_PUBLIC_SITE_URL', 'https://siiqo.com'))


@bridge_bp.route('/payments/initiate-pro-subscription', methods=['POST'])
@jwt_required()
def initiate_pro_subscription():
    """
    Initialise a Paystack subscription checkout.
    Blocks duplicate subscriptions unless the user is upgrading from monthly to annual.
    Returns authorization_url — frontend redirects the user there.
    """
    user_id = get_jwt_identity()
    user = db.session.get(User, int(user_id))
    if not user:
        return jsonify({"message": "Unauthorized"}), 403

    data = request.get_json() or {}
    billing_cycle = data.get('billing_cycle', 'monthly')
    now = _utcnow()

    # --- DUPLICATE PREVENTION ---
    # Check if user already has an active subscription
    existing_sub = VendorSubscription.query.filter_by(
        vendor_id=user.id, status='ACTIVE'
    ).first()

    if existing_sub and existing_sub.end_date > now:
        existing_plan = existing_sub.plan.name if existing_sub.plan else ''
        existing_cycle = 'annual' if 'ANNUAL' in existing_plan else 'monthly'

        # Block if already on same or better plan
        if existing_cycle == billing_cycle:
            return jsonify({
                "message": f"You already have an active {billing_cycle} Pro subscription valid until {existing_sub.end_date.strftime('%d %b %Y')}.",
                "already_subscribed": True,
            }), 409

        if existing_cycle == 'annual':
            return jsonify({
                "message": "You already have an Annual Pro subscription, which is the highest plan.",
                "already_subscribed": True,
            }), 409

        # Allow upgrade: monthly -> annual. Cancel the old monthly first.
        if existing_cycle == 'monthly' and billing_cycle == 'annual':
            existing_sub.status = 'SUPERSEDED'
            db.session.commit()

    # Pick the correct Paystack plan code
    plan_code = PAYSTACK_ANNUAL_PLAN if billing_cycle == 'annual' else PAYSTACK_MONTHLY_PLAN
    logging.info(f'[PAYSTACK] billing_cycle={billing_cycle} plan_code={plan_code!r} key_set={bool(PAYSTACK_SECRET_KEY)}')

    if not PAYSTACK_SECRET_KEY:
        return jsonify({"message": "Payment gateway not configured"}), 503

    if not plan_code and billing_cycle != 'lifetime':
        return jsonify({"message": "Subscription plan not configured"}), 503

    # Determine amount in kobo and plan label
    if billing_cycle == 'annual':
        amount_kobo = 2400000   # ₦24,000
        plan_label  = 'PRO_ANNUAL'
    elif billing_cycle == 'lifetime':
        amount_kobo = 8700000   # ₦87,000
        plan_label  = 'LIFETIME'
    else:
        amount_kobo = 300000    # ₦3,000
        plan_label  = 'PRO_MONTHLY'

    import uuid as _sub_uuid
    ref = f"SUB-{plan_label}-{user_id}-{_sub_uuid.uuid4().hex[:8].upper()}"

    # Call Paystack initialize endpoint
    try:
        payload = {
            "email": user.email,
            "amount": amount_kobo,
            "reference": ref,
            "callback_url": f"{SITE_URL}/payment/subscription-success?plan={plan_label}&ref={ref}",
            "metadata": {
                "user_id": str(user_id),
                "billing_cycle": billing_cycle,
                "plan_name": plan_label,
                "cancel_action": f"{SITE_URL}/finance-tools/upgrade",
            }
        }
        # Only attach a recurring plan code for monthly/annual (not lifetime one-time)
        if plan_code and billing_cycle != 'lifetime':
            payload["plan"] = plan_code

        resp = http_requests.post(
            f"{PAYSTACK_BASE_URL}/transaction/initialize",
            json=payload,
            headers={
                "Authorization": f"Bearer {PAYSTACK_SECRET_KEY}",
                "Content-Type": "application/json",
            },
            timeout=15,
        )
        result = resp.json()
    except Exception as e:
        return jsonify({"message": f"Payment gateway error: {str(e)}"}), 503

    if not resp.ok or not result.get('status'):
        logging.error(f'[PAYSTACK] Error response: {result}')
        return jsonify({
            "message": result.get('message', 'Failed to initialize payment')
        }), 400

    return jsonify({
        "status": "success",
        "data": {
            "authorization_url": result['data']['authorization_url'],
            "access_code": result['data']['access_code'],
            "reference": result['data']['reference'],
        }
    }), 200


@bridge_bp.route('/payments/webhook', methods=['POST'])
def paystack_webhook():
    """
    Receive Paystack webhook events.
    Activates subscription when charge.success fires.
    Paystack signs the payload with HMAC-SHA512 using your secret key.
    """
    # Verify the webhook signature
    signature = request.headers.get('x-paystack-signature', '')
    body = request.get_data()
    expected = hmac.new(
        PAYSTACK_SECRET_KEY.encode('utf-8'),
        body,
        hashlib.sha512
    ).hexdigest()

    if not hmac.compare_digest(expected, signature):
        return jsonify({"message": "Invalid signature"}), 400

    event = request.get_json() or {}
    event_type = event.get('event', '')

    # Handle successful charge (new subscription OR marketplace order)
    if event_type in ('charge.success', 'subscription.create'):
        data = event.get('data', {})
        customer_email = data.get('customer', {}).get('email') or data.get('email', '')
        reference = data.get('reference', '')
        status = data.get('status', '')
        metadata = data.get('metadata', {})

        # ── MARKETPLACE ORDER ──────────────────────────────────────────────
        # Marketplace payments have order_ids in metadata and source=marketplace_checkout.
        # Subscriptions have a plan_code and NO order_ids.
        order_ids_raw = metadata.get('order_ids', [])
        is_marketplace_order = (
            bool(order_ids_raw)
            and metadata.get('source') == 'marketplace_checkout'
        )

        # ── PAY LINK CARD ORDER (PL- prefixed reference) ───────────────────
        # Paystack card payments for Pay Links have reference starting with PL-{order_id}-
        is_paylink_order = reference.startswith('PL-') and status == 'success'

        if is_paylink_order:
            from app.models.order import Order
            from app.models.escrow import EscrowTransaction, EscrowStatus
            from app.models.communication import Notification
            from app.models.payment_link import PaymentLink

            # Extract order_id from reference: PL-{order_id}-{hex}
            try:
                pl_order_id = int(reference.split('-')[1])
            except (IndexError, ValueError):
                pl_order_id = None

            if pl_order_id:
                pl_order = db.session.get(Order, pl_order_id)
                if pl_order and pl_order.status == 'PENDING':
                    pl_escrow = EscrowTransaction.query.filter_by(
                        order_id=pl_order.id, status=EscrowStatus.PENDING_PAYMENT
                    ).first()
                    if pl_escrow:
                        pl_escrow.status = EscrowStatus.IN_ESCROW
                        pl_escrow.paid_at = _utcnow()
                        pl_escrow.payscrow_transaction_id = reference
                        pl_order.status = 'PAID'
                        db.session.flush()

                        link = db.session.get(PaymentLink, pl_order.payment_link_id) if pl_order.payment_link_id else None
                        link_ptype = getattr(link, 'product_type', 'service') or 'service'

                        if link_ptype in ('digital', 'service'):
                            from app.routes.escrow import _deliver_digital_products, _deliver_service_products
                            if not _deliver_digital_products(pl_order, pl_escrow):
                                _deliver_service_products(pl_order, pl_escrow)
                            if link and link.link_type == 'INVOICE':
                                link.status = 'PAID'
                        else:
                            db.session.add(Notification(
                                user_id=pl_order.vendor_id,
                                title="Pay Link Order Paid",
                                message=f"Order #{pl_order.id} via Pay Link has been paid. Prepare for delivery.",
                                type="ESCROW", order_id=pl_order.id,
                            ))
                            db.session.add(Notification(
                                user_id=pl_order.buyer_id,
                                title="Payment Confirmed",
                                message=f"Your payment for Order #{pl_order.id} is confirmed and held in escrow.",
                                type="ORDER", order_id=pl_order.id,
                            ))

                        db.session.commit()
                        logging.info(f"[PAYSTACK WEBHOOK] Pay Link order #{pl_order.id} activated ref={reference}")
            return jsonify({"status": "ok"}), 200
        # ───────────────────────────────────────────────────────────────────

        if is_marketplace_order and status == 'success' and reference:
            from app.models.order import Order
            from app.models.escrow import EscrowTransaction, EscrowStatus
            from app.models.communication import Notification

            order_ids = [int(x) for x in order_ids_raw if str(x).isdigit()]
            orders = Order.query.filter(Order.id.in_(order_ids)).all()
            processed_orders = []

            for order in orders:
                escrow = EscrowTransaction.query.filter_by(
                    transaction_number=reference
                ).filter(
                    EscrowTransaction.order_id == order.id
                ).first()

                if not escrow:
                    # Fallback: match by order_id only
                    escrow = EscrowTransaction.query.filter_by(
                        order_id=order.id,
                        status=EscrowStatus.PENDING_PAYMENT,
                    ).first()

                if escrow and escrow.status == EscrowStatus.PENDING_PAYMENT:
                    escrow.status = EscrowStatus.IN_ESCROW
                    escrow.paid_at = _utcnow()
                    # Store Paystack reference in payscrow_transaction_id for
                    # backward-compat with the release route
                    escrow.payscrow_transaction_id = reference
                    order.status = 'PAID'

                    # Flush so updates are visible to delivery helpers
                    db.session.flush()

                    from app.routes.escrow import _deliver_digital_products, _deliver_service_products, _deliver_event_tickets
                    is_digital = _deliver_digital_products(order, escrow)
                    is_service = False
                    is_event = False
                    if not is_digital:
                        is_service = _deliver_service_products(order, escrow)
                    if not is_digital and not is_service:
                        is_event = _deliver_event_tickets(order, escrow)

                    if not is_digital and not is_service and not is_event:
                        # Physical goods: Activate logistics assignment if pending
                        from app.models.escrow import LogisticsAssignment
                        assignment = LogisticsAssignment.query.filter_by(
                            order_id=order.id
                        ).first()
                        if assignment and assignment.status == 'PENDING':
                            assignment.status = 'ASSIGNED'
                            assignment.assigned_at = _utcnow()
                            db.session.add(Notification(
                                user_id=assignment.partner_id,
                                title="New Delivery Assignment",
                                message=(
                                    f"New delivery for Order #{order.id}. "
                                    f"Fee: ₦{assignment.delivery_fee:,.2f}."
                                ),
                                type="DELIVERY",
                                order_id=order.id,
                            ))

                        db.session.add(Notification(
                            user_id=order.buyer_id,
                            title="Payment Confirmed",
                            message=(
                                f"Your payment for Order #{order.id} is confirmed. "
                                "Funds held by Siiqo until you confirm delivery."
                            ),
                            type="ORDER",
                            order_id=order.id,
                        ))
                        db.session.add(Notification(
                            user_id=order.vendor_id,
                            title="New Paid Order",
                            message=(
                                f"Order #{order.id} has been paid. Please ship the order."
                            ),
                            type="ESCROW",
                            order_id=order.id,
                        ))
                    processed_orders.append(order)

            db.session.commit()

            # Send emails (non-blocking, deduplicated per order)
            from app.utils.email import send_siiqo_email
            for order in processed_orders:
                if getattr(order, 'confirmation_email_sent', False):
                    logging.info(f"[EMAIL DUP GUARD] Order #{order.id} confirmation email already sent. Skipping.")
                    continue

                order.confirmation_email_sent = True
                try:
                    db.session.commit()
                except Exception:
                    db.session.rollback()

                is_digital_or_service = all(
                    (item.product.product_type if item.product else 'physical') in ('digital', 'service')
                    for item in order.items
                )
                buyer = db.session.get(User, order.buyer_id)
                if buyer and buyer.email:
                    try:
                        send_siiqo_email(
                            to_email=buyer.email,
                            subject=f"Order Confirmation #{order.id} - Siiqo",
                            template_name="order_confirmation",
                            first_name=buyer.first_name or "there",
                            order_id=order.id,
                            payment_method="PAYSTACK",
                            is_digital_or_service=is_digital_or_service,
                        )
                    except Exception as e:
                        logging.warning(f"[EMAIL] buyer order confirm failed #{order.id}: {e}")
                vendor = db.session.get(User, order.vendor_id)
                if vendor and vendor.email:
                    try:
                        send_siiqo_email(
                            to_email=vendor.email,
                            subject="New Order - Siiqo",
                            template_name="order_received_vendor",
                            first_name=vendor.first_name or "Vendor",
                            order_id=order.id,
                            total_amount=f"₦{float(order.total_amount):,.2f}",
                            payment_method="PAYSTACK",
                            is_digital_or_service=is_digital_or_service,
                        )
                    except Exception as e:
                        logging.warning(f"[EMAIL] vendor order email failed #{order.id}: {e}")

            logging.info(
                f"[PAYSTACK WEBHOOK] Marketplace orders activated: {order_ids}, ref={reference}"
            )
            return jsonify({"status": "ok"}), 200

        # ── PRO VERIFIED BADGE ────────────────────────────────────────────
        # Pro Verified payments use reference prefix PRO-VER- and have
        # metadata.type = 'pro_verified_subscription'
        is_pro_verified_payment = (
            reference.startswith('PRO-VER-')
            or metadata.get('type') == 'pro_verified_subscription'
        )

        if is_pro_verified_payment and status == 'success':
            storefront_id = metadata.get('storefront_id')
            vendor_id     = metadata.get('vendor_id')
            sf = None
            if storefront_id:
                from app.models.user import Storefront
                sf = db.session.get(Storefront, int(storefront_id))
            elif vendor_id:
                from app.models.user import Storefront
                sf = Storefront.query.filter_by(vendor_id=int(vendor_id)).first()
            elif customer_email:
                user_obj = User.query.filter_by(email=customer_email).first()
                if user_obj:
                    from app.models.user import Storefront
                    sf = user_obj.storefront

            if sf:
                from dateutil.relativedelta import relativedelta
                now_dt = _utcnow()
                # Extend from existing expiry if still valid (allows early renewals)
                base = sf.pro_verified_expires_at if (sf.pro_verified_expires_at and sf.pro_verified_expires_at > now_dt) else now_dt
                sf.is_pro_verified        = True
                sf.pro_verified_expires_at = base + relativedelta(years=1)
                sf.verification_status    = 'VERIFIED'
                db.session.commit()
                logging.info(
                    f'[PAYSTACK] Pro Verified activated for storefront {sf.id} '
                    f'(vendor {sf.vendor_id}) — expires {sf.pro_verified_expires_at}'
                )
                # Notify vendor
                from app.models.communication import Notification
                db.session.add(Notification(
                    user_id=sf.vendor_id,
                    title="Pro Verified Badge Activated! ✅",
                    message=(
                        "Your store now has the Pro Verified badge. "
                        "Buyers can see you are a verified seller on Siiqo."
                    ),
                    type="ACCOUNT",
                ))
                db.session.commit()
            else:
                logging.error(f'[PAYSTACK] Pro Verified payment {reference} — could not find storefront')
            return jsonify({"status": "ok"}), 200

        # ── SUBSCRIPTION ───────────────────────────────────────────────────
        plan_code = (
            data.get('plan', {}).get('plan_code')
            or data.get('plan_code', '')
        )

        if customer_email and status == 'success':
            user = User.query.filter_by(email=customer_email).first()
            if user:
                # Check metadata for plan_name (Lifetime payments embed this since they
                # are one-time transactions, not recurring Paystack plan subscriptions)
                meta_plan_name = metadata.get('plan_name', '')
                is_lifetime = (meta_plan_name == 'LIFETIME' or 'LIFETIME' in reference.upper())

                if is_lifetime:
                    billing_cycle = 'lifetime'
                    plan_name = 'LIFETIME'
                else:
                    # Determine billing cycle from plan code
                    billing_cycle = 'annual' if plan_code == PAYSTACK_ANNUAL_PLAN else 'monthly'
                    plan_name = 'PRO_ANNUAL' if billing_cycle == 'annual' else 'PRO_MONTHLY'

                # Find or create the SubscriptionPlan record
                plan = SubscriptionPlan.query.filter_by(name=plan_name).first()
                if not plan:
                    price_map = {'LIFETIME': 87000, 'PRO_ANNUAL': 24000, 'PRO_MONTHLY': 3000}
                    plan = SubscriptionPlan(
                        name=plan_name,
                        price_ngn=price_map.get(plan_name, 3000),
                        features={"unlimited_invoices": True, "crm": True},
                        is_active=True,
                    )
                    db.session.add(plan)
                    db.session.flush()

                # Supersede any existing active subscriptions
                existing = VendorSubscription.query.filter_by(
                    vendor_id=user.id, status='ACTIVE'
                ).all()
                for sub in existing:
                    sub.status = 'SUPERSEDED'

                # Calculate end date
                from dateutil.relativedelta import relativedelta
                from datetime import datetime as _dt_cls
                now = _utcnow()
                if billing_cycle == 'lifetime':
                    end_date = _dt_cls(2099, 12, 31, 23, 59, 59)  # permanent access
                elif billing_cycle == 'annual':
                    end_date = now + relativedelta(years=1)
                else:
                    end_date = now + relativedelta(months=1)

                # Create new subscription record
                new_sub = VendorSubscription(
                    vendor_id=user.id,
                    plan_id=plan.id,
                    status='ACTIVE',
                    start_date=now,
                    end_date=end_date,
                )
                db.session.add(new_sub)
                db.session.commit()
                logging.info(f'[PAYSTACK] Subscription activated for user {user.id} ({billing_cycle}) until {end_date}')

    # Handle subscription renewal invoice payment
    elif event_type in ('invoice.update', 'invoice.payment_success'):
        data = event.get('data', {})
        customer_email = data.get('customer', {}).get('email', '')
        invoice_status = data.get('status', '')
        if customer_email and invoice_status in ('success', 'paid'):
            user = User.query.filter_by(email=customer_email).first()
            if user:
                from dateutil.relativedelta import relativedelta
                active_sub = VendorSubscription.query.filter_by(
                    vendor_id=user.id, status='ACTIVE'
                ).first()
                if active_sub:
                    # Extend from whichever is later: now or existing end_date (avoids gaps)
                    base = max(active_sub.end_date, _utcnow())
                    active_sub.end_date = base + relativedelta(months=1)
                    db.session.commit()
                    logging.info(f'[PAYSTACK] Subscription renewed for user {user.id} — new end_date: {active_sub.end_date}')

    # Handle subscription cancellation / disable
    elif event_type in ('subscription.not_renew', 'subscription.disable'):
        data = event.get('data', {})
        customer_email = data.get('customer', {}).get('email', '')
        if customer_email:
            user = User.query.filter_by(email=customer_email).first()
            if user:
                VendorSubscription.query.filter_by(
                    vendor_id=user.id, status='ACTIVE'
                ).update({'status': 'CANCELLED'})
                db.session.commit()
                logging.info(f'[PAYSTACK] Subscription cancelled for user {user.id} (event: {event_type})')

    return jsonify({"status": "ok"}), 200


# ===========================================================================
# VENDOR PAYOUTS  (frontend calls /api/vendor/payouts)
# ===========================================================================

@bridge_bp.route('/vendor/payouts', methods=['GET'])
@jwt_required()
def vendor_payouts():
    """Return escrow release history as payout records for the vendor."""
    user_id = get_jwt_identity()
    user = db.session.get(User, int(user_id))
    if not user:
        return jsonify({"message": "Unauthorized"}), 403

    # Get all RELEASED escrow transactions for this vendor's orders
    released = db.session.query(EscrowTransaction).join(
        Order, EscrowTransaction.order_id == Order.id
    ).filter(
        Order.vendor_id == user.id,
        EscrowTransaction.status == EscrowStatus.RELEASED
    ).order_by(EscrowTransaction.released_at.desc()).all()

    payouts = []
    for txn in released:
        order = txn.order
        buyer = db.session.get(User, order.buyer_id) if order else None
        gross = float(txn.amount)
        fee = float(txn.fee_amount or 0)
        net = gross - fee

        payouts.append({
            "id": txn.id,
            "payout_id": txn.transaction_number,
            "order_id": f"ORD-{order.id}" if order else "N/A",
            "transaction_number": txn.transaction_number,
            "buyer_name": buyer.full_name if buyer else "Buyer",
            "gross_amount": gross,
            "platform_fee": fee,
            "net_amount": net,
            "status": "SETTLED" if txn.released_at else "PENDING_SETTLEMENT",
            "released_at": txn.released_at.isoformat() if txn.released_at else None,
            "settled_at": txn.released_at.isoformat() if txn.released_at else None,
        })

    return jsonify({
        "status": "success",
        "data": payouts,
        "total": len(payouts)
    }), 200


# ===========================================================================
# REVENUE STATUS  (frontend calls /api/revenue/status)
# Delegates to finance blueprint
# ===========================================================================

@bridge_bp.route('/revenue/status', methods=['GET'])
def revenue_status_alias():
    from app.routes.finance import revenue_status as _revenue_status
    return _revenue_status()


# ===========================================================================
# REFERRALS
# ===========================================================================

@bridge_bp.route('/referrals/my-stats', methods=['GET'])
@jwt_required()
def my_referral_stats():
    user_id = get_jwt_identity()
    user = db.session.get(User, int(user_id))
    if not user:
        return jsonify({"status": "error", "message": "User not found"}), 404

    referrals = Referral.query.filter_by(referrer_id=user_id).all()
    total_earned = sum(float(r.reward_earned or 0) for r in referrals)

    referred_users = []
    for ref in referrals:
        referred = db.session.get(User, ref.referred_id)
        if referred:
            referred_users.append({
                "name": referred.full_name,
                "joined_at": referred.created_at.strftime('%d %b %Y') if referred.created_at else "Unknown",
                "status": (ref.status or 'pending').lower(),
                "reward_amount": float(ref.reward_earned or 0),
            })

    return jsonify({
        "status": "success",
        "data": {
            "referral_code": user.referral_code or "",
            "total_referred": len(referrals),
            "pending_rewards": sum(float(r.reward_earned or 0) for r in referrals if r.status == 'PENDING'),
            "total_earned": total_earned,
            "total_cash_earned": len(referrals) * 100.00,
            "points_balance": float(user.points_balance or 0),
            "referred_users": referred_users,
        },
    }), 200


# ===========================================================================
# PARTNERS  (frontend calls /api/partners/*)
# ===========================================================================

@bridge_bp.route('/partners/apply', methods=['POST'])
def apply_for_partnership():
    data = request.get_json() or {}
    email = (data.get('email') or '').strip().lower()
    password = data.get('password', '')
    fullname = (data.get('fullname') or '').strip()
    business_name = (data.get('business_name') or fullname or '').strip()

    if not email or not password:
        return jsonify({"message": "Email and password are required"}), 400

    if not business_name:
        return jsonify({"message": "Business name is required"}), 400

    if User.query.filter_by(email=email).first():
        return jsonify({"message": "An account with this email already exists"}), 409

    names = fullname.split(' ', 1)
    new_user = User(
        email=email,
        first_name=names[0],
        last_name=names[1] if len(names) > 1 else '',
        phone=data.get('phone'),
        role=UserRole.PARTNER,
        is_verified=False,
        is_active=True,
    )
    new_user.set_password(password)
    db.session.add(new_user)
    db.session.flush()

    raw_exp = data.get('experience_years') or data.get('experience') or 0
    try:
        exp_years = int("".join(filter(str.isdigit, str(raw_exp))) or 0)
    except Exception:
        exp_years = 0

    partner_app = PartnerApplication(
        user_id=new_user.id,
        business_name=business_name,
        service_type=(data.get('partner_role') or data.get('service_type') or 'LOGISTICS').upper(),
        experience_years=exp_years,
        state_of_operation=data.get('state') or data.get('state_of_operation'),
        status='PENDING',
    )
    db.session.add(partner_app)
    db.session.commit()

    return jsonify({
        "message": "Application submitted. We'll review and get back to you within 48 hours.",
        "status": "success",
    }), 201


@bridge_bp.route('/partners/active', methods=['GET'])
def get_active_partners():
    from app.routes.logistics import get_active_partners as _get_active
    return _get_active()


@bridge_bp.route('/vendor/logistics/settings', methods=['GET', 'POST'])
@jwt_required()
def partner_vendor_logistics_settings():
    user_id = get_jwt_identity()
    user = db.session.get(User, int(user_id))
    if not user:
        return jsonify({"message": "Unauthorized"}), 401

    if user.role == UserRole.PARTNER:
        app = PartnerApplication.query.filter_by(user_id=user.id, status='APPROVED').first()
        if not app:
            return jsonify({"message": "Approved partnership required"}), 403

        if request.method == 'GET':
            pricing = app.pricing_settings or {}
            return jsonify({
                "status": "success",
                "data": {
                    "pricing_model": pricing.get('pricing_model', 'FLAT'),
                    "flat_rate": pricing.get('flat_rate', ''),
                    "base_fee": pricing.get('base_fee', ''),
                    "per_km_fee": pricing.get('per_km_fee', ''),
                    "external_api_key": pricing.get('external_api_key', ''),
                    "bank_code": app.bank_code or '',
                    "account_number": app.account_number or '',
                    "account_name": app.account_name or '',
                }
            }), 200

        data = request.get_json() or {}
        # Build a fresh copy — mutating the existing dict in-place won't trigger
        # SQLAlchemy's change detection for JSON columns.
        pricing = dict(app.pricing_settings or {})
        if 'pricing_model' in data:
            pricing['pricing_model'] = data['pricing_model']
        if 'flat_rate' in data:
            pricing['flat_rate'] = data['flat_rate']
        if 'base_fee' in data:
            pricing['base_fee'] = data['base_fee']
        if 'per_km_fee' in data:
            pricing['per_km_fee'] = data['per_km_fee']
        if 'external_api_key' in data:
            pricing['external_api_key'] = data['external_api_key']
        # Assign the new dict object so SQLAlchemy detects the change
        app.pricing_settings = pricing
        from sqlalchemy.orm.attributes import flag_modified
        flag_modified(app, 'pricing_settings')

        if 'bank_code' in data:
            app.bank_code = data['bank_code']
        if 'account_number' in data:
            app.account_number = data['account_number']
        if 'account_name' in data:
            app.account_name = data['account_name']

        db.session.commit()
        return jsonify({"message": "Settings updated", "status": "success"}), 200

    else:
        # Delegate to standard vendor logistics preferences route
        from app.routes.logistics import vendor_logistics_settings as _vendor_settings
        return _vendor_settings()


@bridge_bp.route('/partners/dashboard/assignments', methods=['GET'])
@jwt_required()
def partner_dashboard_assignments():
    user_id = get_jwt_identity()
    user = db.session.get(User, int(user_id))
    if not user or user.role != UserRole.PARTNER:
        return jsonify({"message": "Partner access required"}), 403

    from app.models.escrow import LogisticsAssignment
    assignments = LogisticsAssignment.query.filter_by(partner_id=user_id).order_by(
        LogisticsAssignment.created_at.desc()
    ).all()

    result = []
    for a in assignments:
        order = a.order
        vendor = order.vendor if order else None
        sf = vendor.storefront if vendor else None

        result.append({
            "id": a.id,
            "order_id": a.order_id,
            "status": a.status,
            "vendor": sf.store_name if sf else (vendor.full_name if vendor else "N/A"),
            "vendor_name": sf.store_name if sf else (vendor.full_name if vendor else "N/A"),
            "vendor_phone": (sf.phone or vendor.phone) if sf else (vendor.phone if vendor else ""),
            "vendor_id": vendor.id if vendor else None,
            "destination": order.delivery_address if order and order.delivery_address else (order.delivery_city if order else "N/A"),
            "delivery_address": order.delivery_address if order else "N/A",
            "city": order.delivery_city if order else "",
            "fee": float(a.delivery_fee),
            "delivery_fee": float(a.delivery_fee),
            "date": a.created_at.strftime('%d %b %Y') if a.created_at else "",
            "created_at": a.created_at.isoformat() if a.created_at else None,
            "buyer_name": order.buyer.full_name if order and order.buyer else "N/A",
            "buyer_phone": order.buyer.phone if order and order.buyer else "",
        })

    return jsonify({"status": "success", "data": result}), 200


@bridge_bp.route('/partners/earnings', methods=['GET'])
@jwt_required()
def partner_earnings():
    """Return real lifetime earnings for a partner from completed deliveries."""
    user_id = get_jwt_identity()
    user = db.session.get(User, int(user_id))
    if not user or user.role != UserRole.PARTNER:
        return jsonify({"message": "Partner access required"}), 403

    from sqlalchemy import func
    from app.models.escrow import LogisticsAssignment

    total_earned = db.session.query(func.sum(LogisticsAssignment.delivery_fee)).filter_by(
        partner_id=user_id, status='DELIVERED'
    ).scalar() or 0.0

    pending_earned = db.session.query(func.sum(LogisticsAssignment.delivery_fee)).filter(
        LogisticsAssignment.partner_id == int(user_id),
        LogisticsAssignment.status.in_(['ASSIGNED', 'IN_TRANSIT']),
    ).scalar() or 0.0

    completed_count = LogisticsAssignment.query.filter_by(
        partner_id=user_id, status='DELIVERED'
    ).count()

    return jsonify({
        "status": "success",
        "total_earned": float(total_earned),
        "pending_earned": float(pending_earned),
        "completed_deliveries": completed_count,
        "currency": "NGN",
    }), 200


@bridge_bp.route('/rider/dashboard', methods=['GET'])
@jwt_required()
def rider_dashboard():
    user_id = get_jwt_identity()
    user = db.session.get(User, int(user_id))
    if not user or user.role != UserRole.RIDER:
        return jsonify({"message": "Rider access required"}), 403

    from app.models.escrow import LogisticsAssignment
    
    # Find assignments where rider's phone number matches
    assignments = LogisticsAssignment.query.filter(
        LogisticsAssignment.rider_phone == user.phone
    ).order_by(LogisticsAssignment.created_at.desc()).all()

    orders_data = []
    for a in assignments:
        order = a.order
        vendor = order.vendor if order else None
        sf = vendor.storefront if vendor else None
        buyer = order.buyer if order else None
        
        orders_data.append({
            "id": order.id if order else a.order_id,
            "pickup_address": sf.address if sf else "N/A",
            "delivery_address": order.delivery_address if order else "N/A",
            "buyer_name": buyer.full_name if buyer else "N/A",
            "buyer_phone": buyer.phone if buyer else "",
            "vendor_name": sf.store_name if sf else (vendor.full_name if vendor else "N/A"),
            "vendor_phone": sf.phone if sf else (vendor.phone if vendor else ""),
            "status": a.status,
        })

    return jsonify({
        "status": "success",
        "rider": {
            "name": user.full_name,
            "email": user.email,
        },
        "orders": orders_data
    }), 200


@bridge_bp.route('/rider/orders/<int:order_id>/deliver', methods=['POST'])
@jwt_required()
def rider_mark_delivered(order_id):
    from datetime import datetime, timezone
    user_id = get_jwt_identity()
    user = db.session.get(User, int(user_id))
    if not user or user.role != UserRole.RIDER:
        return jsonify({"message": "Rider access required"}), 403

    from app.models.escrow import LogisticsAssignment
    assignment = LogisticsAssignment.query.filter_by(
        order_id=order_id, rider_phone=user.phone
    ).first()
    if not assignment:
        return jsonify({"message": "Assignment not found for this rider"}), 404

    data = request.get_json() or {}
    # Secure OTP verification for escrow transactions
    if assignment.order and assignment.order.payment_method == 'ESCROW':
        escrow = assignment.order.escrow
        if escrow:
            provided_otp = str(data.get('delivery_otp') or '').strip()
            actual_otp = str(escrow.escrow_code or '').strip()
            if not actual_otp or provided_otp != actual_otp:
                return jsonify({"message": "Invalid delivery OTP. Please verify with the buyer."}), 400

    # Update assignment and order status to DELIVERED
    assignment.status = 'DELIVERED'
    assignment.delivered_at = datetime.now(timezone.utc)
    if assignment.order and assignment.order.escrow:
        assignment.order.escrow.status = 'DELIVERED'
        
        # Notify buyer
        from app.models.communication import Notification
        db.session.add(Notification(
            user_id=assignment.order.buyer_id,
            title="Your Order Has Been Delivered",
            message=(
                f"Order #{assignment.order.id} has been delivered. "
                "Please confirm receipt to release payment to the vendor."
            ),
            type="ORDER",
            order_id=assignment.order.id,
        ))

    db.session.commit()
    return jsonify({"status": "success", "message": "Order marked as delivered"}), 200

# ===========================================================================
# VENDOR CRYPTO WALLET  — bridge aliases
# Frontend calls /api/vendor/crypto-wallet (vendor_bp prefix is /api/vendor,
# but VendorCryptoWallet routes live in payments_bp at /api/payments).
# These thin aliases keep the frontend URL intact without touching payments.py
# ===========================================================================

@bridge_bp.route('/vendor/crypto-wallet', methods=['GET'])
@jwt_required()
def vendor_crypto_wallet_get():
    """Alias: GET /api/vendor/crypto-wallet → payments.get_vendor_crypto_wallet"""
    from app.routes.payments import get_vendor_crypto_wallet
    return get_vendor_crypto_wallet()


@bridge_bp.route('/vendor/crypto-wallet', methods=['POST'])
@jwt_required()
def vendor_crypto_wallet_post():
    """Alias: POST /api/vendor/crypto-wallet → payments.save_vendor_crypto_wallet"""
    from app.routes.payments import save_vendor_crypto_wallet
    return save_vendor_crypto_wallet()


# ===========================================================================
# SUBSCRIPTION PLANS  — GET /payments/plans
# Returns all available plans so the frontend can show pricing.
# Ensures Lifetime plan exists in the DB on every call (idempotent upsert).
# ===========================================================================

PLAN_CATALOGUE = [
    {
        "name": "FREE",
        "price_ngn": 0,
        "billing": "forever",
        "label": "Starter",
        "tagline": "Get started for free",
        "features": [
            "1 storefront theme",
            "Up to 10 products",
            "3 active Pay Links",
            "7-day analytics",
            "Siiqo escrow on all orders",
            "Standard trust badge",
        ],
        "transaction_fee_pct": 6.0,
        "cta": "Start Free",
        "highlight": False,
    },
    {
        "name": "PRO_MONTHLY",
        "price_ngn": 3000,
        "billing": "monthly",
        "label": "Pro",
        "tagline": "For growing businesses",
        "features": [
            "Unlimited products & catalogs",
            "All storefront themes + customization",
            "Unlimited Pay Links",
            "Full analytics (all time)",
            "Invoice & receipt generator",
            "CRM + customer database",
            "Marketing card generator",
            "3 campaign email blasts/month",
            "Coupon creation & management",
            "Priority marketplace placement",
            "Telegram bot access",
        ],
        "transaction_fee_pct": 6.0,
        "cta": "Start Pro — ₦3,000/mo",
        "highlight": False,
    },
    {
        "name": "PRO_ANNUAL",
        "price_ngn": 24000,
        "billing": "annual",
        "label": "Pro Annual",
        "tagline": "Save ₦12,000 vs monthly",
        "features": [
            "Everything in Pro Monthly",
            "₦12,000 savings per year",
            "Priority support (24h response)",
        ],
        "transaction_fee_pct": 6.0,
        "cta": "Go Annual — ₦24,000/yr",
        "highlight": True,
    },
    {
        "name": "LIFETIME",
        "price_ngn": 87000,
        "billing": "lifetime",
        "label": "Lifetime Access",
        "tagline": "Pay once. Own forever.",
        "features": [
            "Everything in Pro, forever",
            "No monthly or annual fees",
            "Priority support (24h response)",
            "Early access to new features",
            "Exclusive 'Lifetime Member' badge",
        ],
        "transaction_fee_pct": 6.0,
        "cta": "Get Lifetime — ₦87,000",
        "highlight": False,
    },
]


@bridge_bp.route('/payments/plans', methods=['GET'])
def get_plans():
    """Return all subscription plans. No auth required — used on pricing page."""
    # Upsert plans so DB stays in sync with the catalogue
    for plan_def in PLAN_CATALOGUE:
        existing = SubscriptionPlan.query.filter_by(name=plan_def["name"]).first()
        if not existing:            db.session.add(SubscriptionPlan(
                name=plan_def["name"],
                price_ngn=plan_def["price_ngn"],
                features=plan_def["features"],
                is_active=True,
            ))
        else:
            existing.price_ngn = plan_def["price_ngn"]
            existing.features = plan_def["features"]
            existing.is_active = True
    try:
        db.session.commit()
    except Exception:
        db.session.rollback()

    return jsonify({"status": "success", "plans": PLAN_CATALOGUE}), 200


@bridge_bp.route('/payments/subscription', methods=['GET'])
@jwt_required()
def get_subscription_status():
    """Return the current vendor's subscription status."""
    user_id = get_jwt_identity()
    from datetime import datetime as _dt
    now = _dt.utcnow()

    active_sub = VendorSubscription.query.filter(
        VendorSubscription.vendor_id == int(user_id),
        VendorSubscription.status.in_(['ACTIVE', 'CANCELLED_PENDING_EXPIRY']),
        VendorSubscription.end_date > now,
    ).order_by(VendorSubscription.end_date.desc()).first()

    if not active_sub:
        return jsonify({
            "plan": "FREE",
            "status": "inactive",
            "billing_cycle": None,
            "end_date": None,
            "can_upgrade": True,
            "can_cancel": False,
        }), 200

    plan_name = active_sub.plan.name if active_sub.plan else "FREE"
    if "ANNUAL" in plan_name:
        billing_cycle = "annual"
    elif "LIFETIME" in plan_name:
        billing_cycle = "lifetime"
    else:
        billing_cycle = "monthly"

    return jsonify({
        "plan": "PRO" if "PRO" in plan_name or "LIFETIME" in plan_name else "FREE",
        "plan_name": plan_name,
        "status": active_sub.status,
        "billing_cycle": billing_cycle,
        "end_date": active_sub.end_date.isoformat() if active_sub.end_date else None,
        "can_upgrade": billing_cycle == "monthly",
        "can_cancel": active_sub.status == "ACTIVE" and billing_cycle != "lifetime",
    }), 200


@bridge_bp.route('/payments/subscription/cancel', methods=['POST'])
@jwt_required()
def cancel_subscription():
    """Mark vendor subscription as CANCELLED_PENDING_EXPIRY."""
    user_id = get_jwt_identity()
    from datetime import datetime as _dt
    now = _dt.utcnow()

    sub = VendorSubscription.query.filter(
        VendorSubscription.vendor_id == int(user_id),
        VendorSubscription.status == 'ACTIVE',
        VendorSubscription.end_date > now,
    ).first()
    if not sub:
        return jsonify({"message": "No active subscription found"}), 404

    plan_name = sub.plan.name if sub.plan else ""
    if "LIFETIME" in plan_name:
        return jsonify({"message": "Lifetime subscriptions cannot be cancelled"}), 400

    sub.status = 'CANCELLED_PENDING_EXPIRY'
    db.session.commit()
    return jsonify({
        "status": "success",
        "message": "Subscription cancelled. You keep Pro access until your billing period ends.",
    }), 200
