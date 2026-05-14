"""
bridge.py — Route aliases & missing endpoints

Bridges the gap between frontend API call paths and the actual backend
route structure. All routes here either alias existing logic or implement
the missing endpoints the frontend expects at /api/*.

This file is intentionally kept thin — business logic lives in the
dedicated route files. Bridge routes delegate or re-use that logic.
"""
import uuid
from datetime import datetime, timezone

from flask import Blueprint, request, jsonify
from flask_jwt_extended import jwt_required, get_jwt_identity, create_access_token

from app.extensions import db
from app.models.user import User, Storefront, UserRole
from app.models.product import Product, Category, Catalog
from app.models.order import Order, OrderItem, Cart, CartItem
from app.models.escrow import EscrowTransaction, EscrowStatus
from app.models.admin import Favorite
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
    user_id = get_jwt_identity()
    user = db.session.get(User, int(user_id))
    if not user or user.role not in [UserRole.VENDOR, UserRole.ADMIN]:
        return jsonify({"message": "Vendor access required"}), 403

    product = db.session.get(Product, product_id)
    if not product:
        return jsonify({"message": "Product not found"}), 404
    if user.storefront and product.storefront_id != user.storefront.id:
        return jsonify({"message": "Unauthorized"}), 403

    product.is_active = False  # Soft delete
    db.session.commit()
    return jsonify({"message": "Product deleted", "status": "success"}), 200


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

    products = Product.query.filter_by(
        storefront_id=user.storefront.id, is_active=True
    ).all()

    return jsonify({
        "status": "success",
        "catalogs": [{
            "id": user.storefront.id,
            "name": user.storefront.store_name,
            "description": user.storefront.store_description or "",
            "image": user.storefront.store_logo,
            "products": [{
                "id": p.id,
                "name": p.name,
                "price": str(p.price),
                "images": p.images or [],
                "description": p.description,
            } for p in products],
        }],
    }), 200


@bridge_bp.route('/products/catalogs', methods=['POST'])
@jwt_required()
def create_catalog():
    user_id = get_jwt_identity()
    user = db.session.get(User, int(user_id))
    if not user or not user.storefront:
        return jsonify({"message": "Vendor storefront required"}), 403
    return jsonify({"message": "Catalog created", "id": user.storefront.id, "status": "success"}), 201


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
    orders = Order.query.filter_by(buyer_id=user_id).order_by(Order.created_at.desc()).all()

    result = []
    for o in orders:
        vendor = db.session.get(User, o.vendor_id)
        vendor_store = vendor.storefront if vendor else None
        result.append({
            "id": o.id,
            "total": float(o.total_amount),
            "status": o.status,
            "payment_method": "payscrow",
            "date": o.created_at.strftime('%d %b %Y') if o.created_at else "",
            "vendor_name": (
                vendor_store.store_name if vendor_store
                else (vendor.full_name if vendor else "Unknown Vendor")
            ),
            "vendor_id": o.vendor_id,
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
    } for item in (order.items or [])]

    vendor = db.session.get(User, order.vendor_id)
    vendor_store = vendor.storefront if vendor else None
    buyer = db.session.get(User, int(user_id))

    escrow = EscrowTransaction.query.filter_by(order_id=order.id).first()

    return jsonify({
        "status": "success",
        "order": {
            "id": order.id,
            "status": order.status,
            "total_amount": float(order.total_amount),
            "items": items,
            "buyer_name": buyer.full_name if buyer else "",
            "buyer_phone": buyer.phone if buyer else "",
            "vendor_id": order.vendor_id,
            "vendor_name": vendor_store.store_name if vendor_store else "Unknown Vendor",
            "escrow": escrow.to_dict() if escrow else None,
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
    vendor_rating = int(data.get('vendor_rating', 5))
    product_rating = data.get('product_rating')
    review_text = (data.get('review') or data.get('review_text') or '').strip()

    if not order_id:
        return jsonify({"message": "order_id is required"}), 400

    order = db.session.get(Order, order_id)
    if not order or order.buyer_id != int(user_id):
        return jsonify({"message": "Order not found or unauthorized"}), 404

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

    return jsonify({
        "access_token": access_token,
        "partner": {
            "id": user.id,
            "email": user.email,
            "name": user.full_name,
            "partner_role": app_record.service_type if app_record else "LOGISTICS",
            "status": "ACTIVE",
            "wallet_balance": 0,
        },
    }), 200


# ===========================================================================
# PAYMENTS
# ===========================================================================

@bridge_bp.route('/payments/initiate-pro-subscription', methods=['POST'])
@jwt_required()
def initiate_pro_subscription():
    user_id = get_jwt_identity()
    data = request.get_json() or {}
    plan = data.get('plan', 'PRO')
    amount = data.get('amount', 0)

    txn_ref = str(uuid.uuid4())
    return jsonify({
        "status": "success",
        "message": "Subscription initiated",
        "payment_link": f"https://sandbox.payscrow.net/pay/{txn_ref}",
        "transaction_ref": txn_ref,
        "plan": plan,
        "amount": amount,
    }), 200


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

    partner_app = PartnerApplication(
        user_id=new_user.id,
        business_name=business_name,
        service_type=(data.get('partner_role') or data.get('service_type') or 'LOGISTICS').upper(),
        experience_years=int(data.get('experience_years') or data.get('experience') or 0),
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
