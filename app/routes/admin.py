import logging
"""
admin.py — Admin panel routes
All routes require a valid AdminUser JWT.
SUPERADMIN role required for destructive/sensitive operations.
"""
from datetime import datetime, timezone

from flask import Blueprint, request, jsonify
from flask_jwt_extended import jwt_required, get_jwt_identity, create_access_token

from app.extensions import db
from app.models.admin import AdminUser, PlatformSetting, SubscriptionPlan, VendorSubscription
from app.models.user import User, Storefront
from app.models.escrow import EscrowTransaction, EscrowStatus
from app.models.community import Article
from app.models.product import Category
from app.models.partnerships import PartnerApplication
from app.models.finance import Ledger
from app.models.communication import Notification
from app.utils.email import send_siiqo_email

admin_bp = Blueprint('admin', __name__)


def _utcnow():
    return datetime.now(timezone.utc)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _get_admin(admin_id) -> AdminUser | None:
    return db.session.get(AdminUser, int(admin_id))


def _require_superadmin(admin_id) -> AdminUser | None:
    admin = _get_admin(admin_id)
    if not admin or admin.role != 'SUPERADMIN':
        return None
    return admin


def _credit_vendor_ledger(vendor_id: int, amount: float, reference_id: str, description: str):
    from sqlalchemy import func
    credits = db.session.query(func.sum(Ledger.amount)).filter_by(
        vendor_id=vendor_id, transaction_type='CREDIT'
    ).scalar() or 0
    debits = db.session.query(func.sum(Ledger.amount)).filter_by(
        vendor_id=vendor_id, transaction_type='DEBIT'
    ).scalar() or 0
    balance_after = float(credits) - float(debits) + amount
    db.session.add(Ledger(
        vendor_id=vendor_id,
        transaction_type='CREDIT',
        amount=amount,
        description=description,
        reference_id=reference_id,
        balance_after=balance_after,
    ))


# ---------------------------------------------------------------------------
# POST /admin/login
# ---------------------------------------------------------------------------

@admin_bp.route('/login', methods=['POST'])
def admin_login():
    data = request.get_json() or {}
    email = (data.get('email') or '').strip().lower()
    password = data.get('password', '')

    if not email or not password:
        return jsonify({"message": "Email and password required"}), 400

    admin = AdminUser.query.filter_by(email=email).first()
    if not admin or not admin.check_password(password):
        return jsonify({"message": "Invalid credentials"}), 401

    if not admin.is_active:
        return jsonify({"message": "Account suspended"}), 403

    admin.last_login = _utcnow()
    db.session.commit()

    # Use a prefixed identity to distinguish admin tokens from user tokens
    access_token = create_access_token(identity=f"admin:{admin.id}")

    return jsonify({
        "status": "success",
        "access_token": access_token,
        "admin": {
            "id": admin.id,
            "name": admin.name,
            "email": admin.email,
            "role": admin.role,
        },
    }), 200


# ---------------------------------------------------------------------------
# GET /admin/stats
# ---------------------------------------------------------------------------

@admin_bp.route('/stats', methods=['GET'])
@jwt_required()
def admin_dashboard():
    admin_id = get_jwt_identity()
    if not _get_admin(_parse_admin_id(admin_id)):
        return jsonify({"message": "Unauthorized"}), 403

    from app.models.order import Order
    from sqlalchemy import func

    total_users = User.query.count()
    total_vendors = User.query.filter_by(role='VENDOR').count()
    total_storefronts = Storefront.query.count()
    live_storefronts = Storefront.query.filter_by(is_verified=True, is_published=True).count()
    pending_vendors = Storefront.query.filter_by(is_verified=False).count()
    total_orders = Order.query.count()
    total_escrow = EscrowTransaction.query.count()
    pending_escrow = EscrowTransaction.query.filter_by(status=EscrowStatus.PENDING_PAYMENT).count()
    disputed_escrow = EscrowTransaction.query.filter_by(status=EscrowStatus.DISPUTED).count()

    total_gmv = db.session.query(func.sum(Order.total_amount)).filter_by(status='COMPLETED').scalar() or 0
    total_fees = db.session.query(func.sum(EscrowTransaction.fee_amount)).filter_by(
        status=EscrowStatus.RELEASED
    ).scalar() or 0

    return jsonify({
        "total_users": total_users,
        "total_vendors": total_vendors,
        "total_storefronts": total_storefronts,
        "live_storefronts": live_storefronts,
        "pending_vendor_approvals": pending_vendors,
        "total_orders": total_orders,
        "total_escrow_transactions": total_escrow,
        "pending_escrow": pending_escrow,
        "disputed_escrow": disputed_escrow,
        "total_gmv": str(total_gmv),
        "total_platform_fees": str(total_fees),
    }), 200


def _parse_admin_id(identity: str) -> int:
    """Extract numeric ID from 'admin:123' or plain '123'."""
    if isinstance(identity, str) and identity.startswith('admin:'):
        return int(identity.split(':', 1)[1])
    return int(identity)


# ---------------------------------------------------------------------------
# Users
# ---------------------------------------------------------------------------

@admin_bp.route('/users/all', methods=['GET'])
@jwt_required()
def get_all_users():
    admin_id = get_jwt_identity()
    if not _get_admin(_parse_admin_id(admin_id)):
        return jsonify({"message": "Unauthorized"}), 403

    users = User.query.order_by(User.created_at.desc()).limit(500).all()
    user_list = []
    for u in users:
        if u.storefront and not u.storefront.is_verified:
            status = 'pending'
        elif u.is_active and u.is_verified:
            status = 'verified'
        elif not u.is_active:
            status = 'suspended'
        else:
            status = 'unverified'

        sf = u.storefront
        user_list.append({
            "id": u.id,
            "email": u.email,
            "name": u.full_name,
            "phone": u.phone,
            "role": u.role,
            "is_verified": u.is_verified,
            "is_active": u.is_active,
            "status": status,
            "joined_at": u.created_at.isoformat() if u.created_at else None,
            "has_storefront": sf is not None,
            # KYC / Verification fields
            "nin": u.nin,
            "cac_reg": sf.cac_reg if sf else None,
            "account_type": sf.account_type if sf else None,
            "verification_status": sf.verification_status if sf else None,
            "nin_document_url": sf.nin_document_url if sf else None,
            "cac_document_url": sf.cac_document_url if sf else None,
            # Storefront basics
            "store_name": sf.store_name if sf else None,
            "store_logo": sf.store_logo if sf else None,
            "store_slug": sf.store_slug if sf else None,
            "market_presence": {
                "storefront_link": sf.store_slug if sf else None,
                "is_published": sf.is_published if sf else False,
                "product_count": len(sf.products) if sf and sf.products else 0,
                "website": sf.website if sf else None,
            } if sf else None,
        })

    return jsonify({"users": user_list, "count": len(user_list)}), 200


@admin_bp.route('/users/<int:user_id>', methods=['GET'])
@jwt_required()
def get_user_detail(user_id):
    admin_id = get_jwt_identity()
    if not _get_admin(_parse_admin_id(admin_id)):
        return jsonify({"message": "Unauthorized"}), 403

    user = db.session.get(User, user_id)
    if not user:
        return jsonify({"message": "User not found"}), 404

    from app.models.order import Order
    from app.models.partnerships import Referral
    
    orders = Order.query.filter(
        (Order.buyer_id == user.id) | (Order.vendor_id == user.id)
    ).count()

    referrals_query = Referral.query.filter_by(referrer_id=user.id).all()
    referrals_list = [{
        "referred_user_email": r.referred.email if r.referred else "Unknown",
        "referred_user_name": r.referred.full_name if r.referred else "Unknown",
        "status": r.status,
        "created_at": r.created_at.strftime('%Y-%m-%d %H:%M:%S') if r.created_at else "Unknown"
    } for r in referrals_query]

    return jsonify({
        "user": {
            **user.to_public_dict(),
            "total_orders": orders,
            "storefront": user.storefront.to_public_dict() if user.storefront else None,
            "referrals_made": referrals_list,
        }
    }), 200


@admin_bp.route('/users/<int:user_id>/status', methods=['PATCH'])
@jwt_required()
def update_user_status(user_id):
    admin_id = get_jwt_identity()
    if not _require_superadmin(_parse_admin_id(admin_id)):
        return jsonify({"message": "SuperAdmin required"}), 403

    user = db.session.get(User, user_id)
    if not user:
        return jsonify({"message": "User not found"}), 404

    data = request.get_json() or {}
    status = data.get('status', '')

    if status in ('active', 'verified', 'approved'):
        # Stage 1: Admin approves vendor to go public. Does NOT grant the Verified badge.
        user.is_verified = True
        user.is_active = True
        if user.storefront:
            user.storefront.is_verified = True
            # verification_status is intentionally NOT changed here.
            # The Verified badge is granted separately via 'kyc_approved'.
            try:
                send_siiqo_email(
                    to_email=user.email,
                    subject="Congratulations! Your Siiqo Store is Approved",
                    template_name="vendor_approved",
                    first_name=user.first_name or "Vendor",
                    store_name=user.storefront.store_name,
                )
            except Exception:
                pass

    elif status == 'kyc_approved':
        # Stage 2: Admin approves submitted KYC (NIN/CAC). Grants the Verified badge.
        # Also grants Pro Verified (is_pro_verified) at no charge — grandfathering
        # vendors who verified before the paid Pro Verified system was introduced.
        user.is_verified = True
        user.is_active = True
        if user.storefront:
            user.storefront.is_verified = True
            user.storefront.verification_status = 'VERIFIED'
            # Grandfather: give them Pro Verified badge for free (1 year)
            from datetime import timedelta as _td
            from datetime import datetime as _dt_kyc
            user.storefront.is_pro_verified = True
            user.storefront.pro_verified_expires_at = _dt_kyc.utcnow() + _td(days=365)
            try:
                send_siiqo_email(
                    to_email=user.email,
                    subject="Your Identity is Verified — Siiqo",
                    template_name="vendor_approved",
                    first_name=user.first_name or "Vendor",
                    store_name=user.storefront.store_name,
                )
            except Exception:
                pass
    elif status == 'suspended':
        user.is_active = False
        if user.storefront:
            try:
                send_siiqo_email(
                    to_email=user.email,
                    subject="Important: Your Siiqo Storefront has been suspended",
                    template_name="vendor_suspended",
                    first_name=user.first_name or "Vendor",
                    store_name=user.storefront.store_name,
                )
            except Exception:
                pass
    elif status == 'rejected':
        if user.storefront:
            user.storefront.verification_status = 'REJECTED'
            user.storefront.is_verified = False
            try:
                send_siiqo_email(
                    to_email=user.email,
                    subject="Verification Update: Siiqo Application Status",
                    template_name="vendor_verification_rejected",
                    first_name=user.first_name or "Vendor",
                    store_name=user.storefront.store_name,
                )
            except Exception:
                pass
    elif status == 'unverified':
        user.is_verified = False
        if user.storefront:
            user.storefront.verification_status = 'NOT_SUBMITTED'

    db.session.commit()

    # Recalculate trust score for storefront verification changes
    try:
        from app.services.trust import recalculate_vendor_trust
        if user.role == 'VENDOR':
            recalculate_vendor_trust(user.id, reason="Admin Verification Update")
    except Exception as e:
        logging.error(f"[TRUST ERROR] Failed to recalculate trust on user status update: {e}")

    return jsonify({"message": f"User {user.email} status updated to {status}."}), 200


@admin_bp.route('/users/<int:user_id>', methods=['DELETE'])
@jwt_required()
def delete_user_admin(user_id):
    admin_id = get_jwt_identity()
    if not _require_superadmin(_parse_admin_id(admin_id)):
        return jsonify({"message": "SuperAdmin required"}), 403

    user = db.session.get(User, user_id)
    if not user:
        return jsonify({"message": "User not found"}), 404

    try:
        from app.models.escrow import EscrowTransaction as ET
        from app.models.order import Order, OrderItem

        # Delete escrow transactions linked to user's orders
        orders = Order.query.filter(
            (Order.buyer_id == user.id) | (Order.vendor_id == user.id)
        ).all()
        for order in orders:
            ET.query.filter_by(order_id=order.id).delete(synchronize_session=False)
            OrderItem.query.filter_by(order_id=order.id).delete(synchronize_session=False)
            db.session.delete(order)

        # Delete products
        if user.storefront:
            from app.models.product import Product
            Product.query.filter_by(storefront_id=user.storefront.id).delete(synchronize_session=False)
            db.session.delete(user.storefront)

        db.session.delete(user)
        db.session.commit()
        return jsonify({"message": f"User {user.email} and all data deleted."}), 200
    except Exception as e:
        db.session.rollback()
        return jsonify({"message": f"Failed to delete user: {str(e)}"}), 500


# ---------------------------------------------------------------------------
# Storefronts
# ---------------------------------------------------------------------------

@admin_bp.route('/storefronts', methods=['GET'])
@jwt_required()
def get_all_storefronts():
    admin_id = get_jwt_identity()
    if not _get_admin(_parse_admin_id(admin_id)):
        return jsonify({"message": "Unauthorized"}), 403

    status = request.args.get('status')
    query = Storefront.query
    if status:
        query = query.filter_by(verification_status=status)

    storefronts = query.order_by(Storefront.created_at.desc()).limit(500).all()
    return jsonify({
        "storefronts": [{
            "id": s.id,
            "business_name": s.store_name,
            "store_slug": s.store_slug,
            "vendor_name": s.vendor.full_name if s.vendor else "Unknown",
            "vendor_email": s.vendor.email if s.vendor else "",
            "phone": s.phone or (s.vendor.phone if s.vendor else None),
            "is_verified": s.is_verified,
            "is_published": s.is_published,
            "is_live": s.is_live,
            "vendor_status": "Approved" if s.is_verified else "Pending",
            "city": s.city,
            "state": s.state,
            "created_at": s.created_at.isoformat() if s.created_at else None,
            # verification details
            "account_type": s.account_type,
            "cac_reg": s.cac_reg,
            "nin": s.vendor.nin if s.vendor else None,
            "nin_document_url": s.nin_document_url,
            "cac_document_url": s.cac_document_url,
            "verification_status": s.verification_status,
        } for s in storefronts],
        "count": len(storefronts),
    }), 200


# ---------------------------------------------------------------------------
# Categories
# ---------------------------------------------------------------------------

@admin_bp.route('/categories', methods=['GET', 'POST'])
@jwt_required()
def handle_categories():
    admin_id = get_jwt_identity()
    if not _get_admin(_parse_admin_id(admin_id)):
        return jsonify({"message": "Unauthorized"}), 403

    if request.method == 'GET':
        cats = Category.query.limit(500).all()
        return jsonify({
            "categories": [{
                "id": c.id,
                "name": c.name,
                "slug": c.slug,
                "icon": c.icon,
                "attribute_schema": c.attribute_schema or [],
                "product_type_hint": c.product_type_hint or [],
            } for c in cats],
            "count": len(cats),
        }), 200

    if not _require_superadmin(_parse_admin_id(admin_id)):
        return jsonify({"message": "SuperAdmin required"}), 403

    data = request.get_json() or {}
    name = (data.get('name') or '').strip()
    if not name:
        return jsonify({"message": "Category name is required"}), 400

    import re
    slug = data.get('slug') or re.sub(r'[^a-z0-9]+', '-', name.lower()).strip('-')

    if Category.query.filter_by(slug=slug).first():
        return jsonify({"message": "Category already exists"}), 409

    cat = Category(
        name=name,
        slug=slug,
        icon=data.get('icon'),
        attribute_schema=data.get('attribute_schema'),
        product_type_hint=data.get('product_type_hint'),
    )
    db.session.add(cat)
    db.session.commit()
    return jsonify({"message": f"Category '{name}' created.", "id": cat.id, "slug": cat.slug}), 201


@admin_bp.route('/categories/<int:cat_id>', methods=['PATCH', 'DELETE'])
@jwt_required()
def manage_category(cat_id):
    admin_id = get_jwt_identity()
    if not _require_superadmin(_parse_admin_id(admin_id)):
        return jsonify({"message": "SuperAdmin required"}), 403

    cat = db.session.get(Category, cat_id)
    if not cat:
        return jsonify({"message": "Category not found"}), 404

    if request.method == 'DELETE':
        from app.models.product import Product as Prod
        product_count = Prod.query.filter_by(category_id=cat_id).count()
        force = request.args.get('force', 'false').lower() == 'true'
        if product_count > 0 and not force:
            return jsonify({
                "message": f"Cannot delete '{cat.name}'. It has {product_count} product(s) assigned. "
                           f"Pass ?force=true to delete anyway (products will become uncategorized).",
                "product_count": product_count,
                "requires_force": True,
            }), 409
        if product_count > 0 and force:
            Prod.query.filter_by(category_id=cat_id).update({"category_id": None})
        db.session.delete(cat)
        db.session.commit()
        return jsonify({"message": f"Category '{cat.name}' deleted.", "affected_products": product_count}), 200

    data = request.get_json() or {}
    if 'name' in data:
        cat.name = data['name']
    if 'slug' in data:
        cat.slug = data['slug']
    # New enrichment fields — safe to update independently
    if 'icon' in data:
        cat.icon = data['icon']
    if 'attribute_schema' in data:
        cat.attribute_schema = data['attribute_schema']
    if 'product_type_hint' in data:
        cat.product_type_hint = data['product_type_hint']
    db.session.commit()
    return jsonify({"message": "Category updated.", "id": cat.id}), 200


# ---------------------------------------------------------------------------
# Escrow Management
# ---------------------------------------------------------------------------

@admin_bp.route('/escrow/pending', methods=['GET'])
@jwt_required()
def get_pending_escrow():
    admin_id = get_jwt_identity()
    if not _get_admin(_parse_admin_id(admin_id)):
        return jsonify({"message": "Unauthorized"}), 403

    from app.models.order import Order, OrderItem

    pending = EscrowTransaction.query.filter(
        EscrowTransaction.status.in_([
            EscrowStatus.PENDING_PAYMENT,
            EscrowStatus.IN_ESCROW,
            EscrowStatus.DISPUTED,
        ])
    ).order_by(EscrowTransaction.created_at.desc()).limit(500).all()

    result = []
    for e in pending:
        order = e.order
        buyer = db.session.get(User, order.buyer_id) if order else None
        vendor = db.session.get(User, order.vendor_id) if order else None
        vendor_store = vendor.storefront if vendor else None

        items = []
        if order:
            for item in order.items:
                items.append({
                    "name": item.product.name if item.product else "Unknown Product",
                    "quantity": item.quantity,
                    "price": float(item.price_at_purchase),
                })

        result.append({
            **e.to_dict(),
            # Richer fields for dispute resolution
            "order_id": order.id if order else None,
            "order_status": order.status if order else None,
            "order_created_at": order.created_at.isoformat() if order and order.created_at else None,
            "payment_method": order.payment_method if order else None,
            # Buyer
            "buyer_id": buyer.id if buyer else None,
            "buyer_name": buyer.full_name if buyer else "Unknown",
            "buyer_email": buyer.email if buyer else "",
            # Vendor
            "vendor_id": vendor.id if vendor else None,
            "vendor_name": vendor_store.store_name if vendor_store else (vendor.full_name if vendor else "Unknown"),
            "vendor_email": vendor.email if vendor else "",
            # Items
            "items": items,
            "total_amount": float(e.amount),
        })

    return jsonify({
        "pending_escrow": result,
        "count": len(result),
    }), 200


@admin_bp.route('/escrow/transactions', methods=['GET'])
@jwt_required()
def get_all_escrow_transactions():
    """
    Returns ALL escrow transactions for the admin escrow dashboard.
    Normalises data into the shape the frontend AdminEscrowDashboard expects:
      { id, vendor_id, buyer_id, product, total_amount, platform_fee,
        status ("held" | "disputed" | "released" | "refunded"),
        created_at, auto_release_date, logistics_provider }
    """
    admin_id = get_jwt_identity()
    if not _get_admin(_parse_admin_id(admin_id)):
        return jsonify({"message": "Unauthorized"}), 403

    from app.models.order import Order, OrderItem
    from app.models.product import Product
    from datetime import timedelta

    # Status map: backend canonical → frontend display key
    STATUS_MAP = {
        EscrowStatus.PENDING_PAYMENT: "held",
        EscrowStatus.IN_ESCROW:       "held",
        EscrowStatus.SHIPPED:         "held",
        EscrowStatus.DELIVERED:       "held",
        EscrowStatus.RELEASED:        "released",
        EscrowStatus.DISPUTED:        "disputed",
        EscrowStatus.REFUNDED:        "refunded",
        EscrowStatus.CANCELLED:       "refunded",
    }

    transactions = EscrowTransaction.query.order_by(
        EscrowTransaction.created_at.desc()
    ).limit(1000).all()

    result = []
    for txn in transactions:
        order = txn.order
        if not order:
            continue

        # Derive a human-readable product name from first order item
        first_item = order.items[0] if order.items else None
        product_name = (
            first_item.product.name if first_item and first_item.product
            else f"Order #{order.id}"
        )

        # Auto-release date = updated_at + 72 h when status is DELIVERED
        auto_release_date = None
        if txn.status == EscrowStatus.DELIVERED and txn.updated_at:
            auto_release_date = (txn.updated_at + timedelta(hours=72)).isoformat()

        result.append({
            "id": txn.transaction_number,
            "order_id": order.id,
            "vendor_id": str(order.vendor_id),
            "buyer_id": str(order.buyer_id),
            "product": product_name,
            "total_amount": float(txn.amount),
            "platform_fee": float(txn.fee_amount or 0),
            "status": STATUS_MAP.get(txn.status, "held"),
            "escrow_status": txn.status,   # raw backend status for admin detail
            "created_at": txn.created_at.isoformat() if txn.created_at else None,
            "auto_release_date": auto_release_date,
            "logistics_provider": order.logistics_provider_id or "Standard Delivery",
            "dispute_id": txn.dispute_id,
            "dispute_reason": txn.dispute_reason,
        })

    return jsonify({"data": result, "count": len(result)}), 200


@admin_bp.route('/escrow/refund/<int:order_id>', methods=['POST'])
@jwt_required()
def admin_refund_buyer(order_id):
    """
    Admin refunds the buyer (dispute resolved in buyer's favour).
    Calls PayScrow's broker refund endpoint to reverse the transaction,
    then marks the escrow as REFUNDED and notifies both parties.
    """
    admin_id = get_jwt_identity()
    if not _require_superadmin(_parse_admin_id(admin_id)):
        return jsonify({"message": "SuperAdmin required"}), 403

    escrow = EscrowTransaction.query.filter_by(order_id=order_id).first()
    if not escrow:
        return jsonify({"message": "Escrow transaction not found"}), 404

    if escrow.status == EscrowStatus.REFUNDED:
        return jsonify({"message": "Funds already refunded"}), 400

    if escrow.status == EscrowStatus.RELEASED:
        return jsonify({"message": "Funds already released to vendor — cannot refund"}), 400

    # Call PayScrow broker refund / cancel endpoint
    if escrow.payscrow_ref:
        import os, requests as _requests
        payscrow_key = os.environ.get('PAYSCROW_API_KEY', '')
        base_url = os.environ.get('PAYSCROW_BASE_URL')
        if not base_url:
            _is_sandbox = (
                not payscrow_key
                or payscrow_key.startswith('ps_9')
                or os.environ.get('PAYSCROW_ENV', '').lower() == 'sandbox'
            )
            base_url = "https://api.payscrow.dev" if _is_sandbox else "https://api.payscrow.net"
        headers = {"BrokerApiKey": payscrow_key, "Content-Type": "application/json"}
        try:
            resp = _requests.post(
                f"{base_url}/api/v3/marketplace/transactions/{escrow.payscrow_ref}/broker/refund",
                json={"reason": "Admin dispute resolution — refund to buyer"},
                headers=headers,
                timeout=15,
            )
            resp_data = resp.json()
            if not resp_data.get('success'):
                logging.warning(
                    f"[ADMIN REFUND] PayScrow refund returned non-success for Order #{order_id}: {resp.text}"
                )
                # Continue — still update our DB so order isn't stuck
        except Exception as e:
            logging.error(f"[ADMIN REFUND] PayScrow refund error for Order #{order_id}: {e}")
    else:
        logging.warning(
            f"[ADMIN REFUND] Order #{order_id} has no payscrow_ref — skipping PayScrow call."
        )

    escrow.status = EscrowStatus.REFUNDED
    order = escrow.order
    if order:
        if order.status != 'CANCELLED':
            for item in order.items:
                if item.product:
                    item.product.stock_quantity = (item.product.stock_quantity or 0) + item.quantity
        order.status = 'CANCELLED'

    # Notify buyer — they get their money back
    if order:
        db.session.add(Notification(
            user_id=order.buyer_id,
            title="Refund Processed",
            message=f"Your dispute for Order #{order_id} was resolved in your favour. A refund of ₦{float(escrow.amount):,.2f} has been initiated.",
            type="ESCROW",
            order_id=order_id,
        ))
        # Notify vendor — they don't get paid
        db.session.add(Notification(
            user_id=order.vendor_id,
            title="Dispute Resolved — Refund Issued",
            message=f"The dispute for Order #{order_id} was resolved in the buyer's favour. Funds have been refunded to the buyer.",
            type="ESCROW",
            order_id=order_id,
        ))

    db.session.commit()

    # Trigger trust score recalculation on dispute lost
    try:
        from app.services.trust import recalculate_vendor_trust
        if order:
            recalculate_vendor_trust(order.vendor_id, reason="Dispute Resolved (Refunded)")
    except Exception as e:
        logging.error(f"[TRUST ERROR] Failed to recalculate trust on admin refund: {e}")

    return jsonify({"message": "Refund processed successfully.", "status": "success"}), 200


@admin_bp.route('/escrow/verify/<int:order_id>', methods=['POST'])
@jwt_required()
def admin_verify_payment(order_id):
    """Admin manually marks a payment as received (for bank transfer cases)."""


# ---------------------------------------------------------------------------
# POST /admin/escrow/fix-crypto-order/<order_id>
# Marks a crypto order as COMPLETED when it got stuck in PENDING due to
# a backend error (e.g. Notification import crash on Order #226-228).
# ---------------------------------------------------------------------------

@admin_bp.route('/escrow/fix-crypto-order/<int:order_id>', methods=['POST'])
@jwt_required()
def admin_fix_crypto_order(order_id):
    """
    Admin route to fix crypto orders stuck in PENDING status.
    Marks the order and its EscrowTransaction as COMPLETED/RELEASED.
    Use when order was paid via Daya but backend crashed before committing.
    """
    from app.models.order import Order
    admin_id = get_jwt_identity()
    if not _get_admin(_parse_admin_id(admin_id)):
        return jsonify({"message": "Unauthorized"}), 403

    order = db.session.get(Order, order_id)
    if not order:
        return jsonify({"message": f"Order #{order_id} not found"}), 404

    if order.payment_method not in ("CRYPTO", "crypto"):
        return jsonify({"message": f"Order #{order_id} is not a crypto order"}), 400

    escrow = EscrowTransaction.query.filter_by(order_id=order_id).first()
    if not escrow:
        return jsonify({"message": f"No escrow record for Order #{order_id}"}), 404

    old_order_status  = order.status
    old_escrow_status = escrow.status

    # Mark order as COMPLETED
    order.status = 'COMPLETED'

    # Mark escrow as RELEASED
    from datetime import datetime, timezone
    escrow.status      = EscrowStatus.RELEASED
    escrow.released_at = escrow.released_at or datetime.now(timezone.utc)

    # Send notifications
    db.session.add(Notification(
        user_id=order.buyer_id,
        title="Order Completed",
        message=f"Your crypto payment for Order #{order_id} is confirmed and the order is complete.",
        type="ORDER",
        order_id=order_id,
    ))
    db.session.add(Notification(
        user_id=order.vendor_id,
        title="Order Complete",
        message=f"Order #{order_id} has been marked complete by admin.",
        type="ESCROW",
        order_id=order_id,
    ))

    db.session.commit()

    logging.info(
        "[ADMIN FIX] Order #%s status %s->COMPLETED, escrow %s->RELEASED",
        order_id, old_order_status, old_escrow_status
    )

    return jsonify({
        "message": f"Order #{order_id} fixed successfully",
        "order_status": "COMPLETED",
        "escrow_status": "RELEASED",
        "previous_order_status": old_order_status,
        "previous_escrow_status": old_escrow_status,
    }), 200
    admin_id = get_jwt_identity()
    if not _require_superadmin(_parse_admin_id(admin_id)):
        return jsonify({"message": "SuperAdmin required"}), 403

    escrow = EscrowTransaction.query.filter_by(order_id=order_id).first()
    if not escrow:
        return jsonify({"message": "Escrow transaction not found"}), 404

    escrow.status = EscrowStatus.IN_ESCROW
    escrow.paid_at = _utcnow()
    if escrow.order:
        escrow.order.status = 'PAID'
    db.session.commit()
    return jsonify({"message": "Payment verified. Escrow is now active.", "status": "success"}), 200


@admin_bp.route('/escrow/release/<int:order_id>', methods=['POST'])
@jwt_required()
def admin_release_funds(order_id):
    """Admin force-releases escrow funds (e.g., after dispute resolution)."""
    admin_id = get_jwt_identity()
    if not _require_superadmin(_parse_admin_id(admin_id)):
        return jsonify({"message": "SuperAdmin required"}), 403

    escrow = EscrowTransaction.query.filter_by(order_id=order_id).first()
    if not escrow:
        return jsonify({"message": "Escrow transaction not found"}), 404

    if escrow.status == EscrowStatus.RELEASED:
        return jsonify({"message": "Funds already released"}), 400

    # Call PayScrow's applycode to actually move the money — required for DISPUTED orders
    # where funds are frozen on PayScrow's side.
    if escrow.payscrow_transaction_id and escrow.escrow_code:
        import os, requests as _requests
        payscrow_key = os.environ.get('PAYSCROW_API_KEY', '')
        base_url = os.environ.get('PAYSCROW_BASE_URL')
        if not base_url:
            _is_sandbox = (
                not payscrow_key
                or payscrow_key.startswith('ps_9')
                or os.environ.get('PAYSCROW_ENV', '').lower() == 'sandbox'
            )
            base_url = "https://api.payscrow.dev" if _is_sandbox else "https://api.payscrow.net"
        headers = {"BrokerApiKey": payscrow_key, "Content-Type": "application/json"}
        try:
            resp = _requests.post(
                f"{base_url}/api/v3/escrow/escrowtransactions/applycode",
                json={"transactionId": escrow.payscrow_transaction_id, "code": escrow.escrow_code},
                headers=headers,
                timeout=15,
            )
            resp_data = resp.json()
            if not resp_data.get('success'):
                logging.warning(
                    f"[ADMIN RELEASE] PayScrow applycode returned non-success for Order #{order_id}: {resp.text}"
                )
                # Do NOT hard-fail — admin may be resolving a dispute where PayScrow
                # already released on their side. Continue with DB update.
        except Exception as e:
            logging.error(f"[ADMIN RELEASE] PayScrow applycode error for Order #{order_id}: {e}")
            # Non-fatal: still update our DB so the order is not stuck forever.
    else:
        logging.warning(
            f"[ADMIN RELEASE] Order #{order_id} has no payscrow_transaction_id or escrow_code — "
            "skipping PayScrow applycode call (manual bank transfer or missing data)."
        )

    escrow.status = EscrowStatus.RELEASED
    escrow.released_at = _utcnow()
    order = escrow.order
    if order:
        order.status = 'COMPLETED'
        net_amount = float(escrow.amount) - float(escrow.fee_amount or 0)
        _credit_vendor_ledger(
            vendor_id=order.vendor_id,
            amount=net_amount,
            reference_id=escrow.transaction_number,
            description=f"Admin-released payout for Order #{order.id}",
        )
        db.session.add(Notification(
            user_id=order.vendor_id,
            title="Funds Released by Admin",
            message=f"₦{net_amount:,.2f} has been credited to your ledger for Order #{order.id}.",
            type="ESCROW",
            order_id=order.id,
        ))
        # Also notify buyer
        db.session.add(Notification(
            user_id=order.buyer_id,
            title="Order Complete",
            message=f"Order #{order.id} has been resolved by Siiqo support. The order is now complete.",
            type="ORDER",
            order_id=order.id,
        ))

    db.session.commit()

    # Trigger trust score recalculation on dispute won / admin release
    try:
        from app.services.trust import recalculate_vendor_trust
        if order:
            recalculate_vendor_trust(order.vendor_id, reason="Dispute Resolved (Released)")
    except Exception as e:
        logging.error(f"[TRUST ERROR] Failed to recalculate trust on admin release: {e}")

    return jsonify({"message": "Funds released successfully.", "status": "success"}), 200


# ---------------------------------------------------------------------------
# Partners
# ---------------------------------------------------------------------------

@admin_bp.route('/partners/applications', methods=['GET'])
@jwt_required()
def get_partnerships():
    admin_id = get_jwt_identity()
    if not _get_admin(_parse_admin_id(admin_id)):
        return jsonify({"message": "Unauthorized"}), 403

    applications = PartnerApplication.query.order_by(PartnerApplication.applied_at.desc()).limit(500).all()
    return jsonify({
        "partners": [{
            "id": p.id,
            "business_name": p.business_name,
            "service_type": p.service_type,
            "partner_role": p.service_type,         # alias so frontend field names work
            "status": p.status,
            "state_of_operation": p.state_of_operation,
            "experience_years": p.experience_years,
            "portfolio_url": p.portfolio_url,
            "applied_at": p.applied_at.isoformat() if p.applied_at else None,
            "reviewed_at": p.reviewed_at.isoformat() if p.reviewed_at else None,
            # Bank payout details
            "bank_name": p.account_name or "",
            "account_number": p.account_number or "",
            "account_name": p.account_name or "",
            "bank_code": p.bank_code or "",
            # Applicant contact — use both field name conventions so old + new frontend works
            "applicant": {
                "name": p.user.full_name if p.user else "Unknown",
                "email": p.user.email if p.user else "",
                "phone": p.user.phone if p.user else "",
            },
            "contact_email": p.user.email if p.user else "",
            "contact_phone": p.user.phone if p.user else "",
            "user_id": p.user.id if p.user else None,
        } for p in applications],
        "count": len(applications),
    }), 200


@admin_bp.route('/partners/<int:app_id>/status', methods=['PATCH'])
@jwt_required()
def update_partner_status(app_id):
    admin_id = get_jwt_identity()
    if not _require_superadmin(_parse_admin_id(admin_id)):
        return jsonify({"message": "SuperAdmin required"}), 403

    app = db.session.get(PartnerApplication, app_id)
    if not app:
        return jsonify({"message": "Application not found"}), 404

    data = request.get_json() or {}
    status = (data.get('status') or '').upper()

    if status == 'APPROVED':
        app.status = 'APPROVED'
        app.reviewed_at = _utcnow()
        if app.user:
            app.user.role = 'PARTNER'
            app.user.is_verified = True
            try:
                send_siiqo_email(
                    to_email=app.user.email,
                    subject="Welcome to the Siiqo Partner Network!",
                    template_name="system_notice",
                    header="Application Approved",
                    body=(
                        f"Hi {app.user.full_name}, your partner application for {app.business_name} "
                        "has been approved. You can now log in to your partner dashboard."
                    ),
                )
            except Exception:
                pass
    elif status in ('REJECTED', 'ACTIVE', 'SUSPENDED'):
        app.status = status
        app.reviewed_at = _utcnow()
    else:
        return jsonify({"message": "Invalid status"}), 400

    db.session.commit()
    return jsonify({"message": f"Partner application {status.lower()}.", "status": "success"}), 200


# Backward-compat alias
@admin_bp.route('/partnerships/<int:app_id>/approve', methods=['POST'])
@jwt_required()
def approve_partnership(app_id):
    admin_id = get_jwt_identity()
    if not _require_superadmin(_parse_admin_id(admin_id)):
        return jsonify({"message": "SuperAdmin required"}), 403

    app = db.session.get(PartnerApplication, app_id)
    if not app:
        return jsonify({"message": "Application not found"}), 404

    app.status = 'APPROVED'
    app.reviewed_at = _utcnow()
    if app.user:
        app.user.role = 'PARTNER'
        app.user.is_verified = True
    db.session.commit()
    return jsonify({"message": f"Partnership {app.business_name} approved."}), 200


# ---------------------------------------------------------------------------
# Blog
# ---------------------------------------------------------------------------

@admin_bp.route('/blog', methods=['GET', 'POST'])
@jwt_required()
def handle_blog():
    admin_id = get_jwt_identity()
    parsed_id = _parse_admin_id(admin_id)
    admin = _get_admin(parsed_id)
    if not admin:
        return jsonify({"message": "Unauthorized"}), 403

    if request.method == 'GET':
        articles = Article.query.order_by(Article.created_at.desc()).limit(500).all()
        return jsonify({
            "articles": [{
                "id": a.id,
                "title": a.title,
                "slug": a.slug,
                "category": a.category,
                "excerpt": a.excerpt,
                "cover_image": a.cover_image,
                "status": "published" if a.is_published else "draft",
                "is_published": a.is_published,
                "created_at": a.created_at.isoformat() if a.created_at else None,
            } for a in articles]
        }), 200

    if not _require_superadmin(parsed_id):
        return jsonify({"message": "SuperAdmin required"}), 403

    import re

    # Support both JSON and multipart/form-data (for image uploads)
    if request.content_type and 'multipart' in request.content_type:
        data = request.form.to_dict()
        cover_image_file = request.files.get('cover_image')
        cover_image_url = None
        if cover_image_file:
            import os, uuid
            from app.utils.upload import save_uploaded_file
            s3_url = save_uploaded_file(cover_image_file, subfolder="blog")
            if s3_url:
                cover_image_url = s3_url
    else:
        data = request.get_json() or {}
        cover_image_url = data.get('cover_image')

    title = data.get('title', '')
    if not title:
        return jsonify({"message": "Title is required"}), 400
    slug = data.get('slug') or re.sub(r'[^a-z0-9]+', '-', title.lower()).strip('-')

    # Ensure slug is unique
    original_slug = slug
    import uuid
    while Article.query.filter_by(slug=slug).first():
        slug = f"{original_slug}-{str(uuid.uuid4())[:6]}"

    new_article = Article(
        admin_author_id=parsed_id,
        title=title,
        slug=slug,
        category=data.get('category'),
        content=data.get('content', ''),
        excerpt=data.get('excerpt'),
        cover_image=cover_image_url,
        is_published=data.get('is_published', False) in (True, 'true', '1', 'published'),
        meta_title=data.get('meta_title') or data.get('seo_title'),
        meta_description=data.get('meta_description') or data.get('seo_description'),
    )
    db.session.add(new_article)
    db.session.commit()
    return jsonify({"message": "Article created.", "id": new_article.id, "slug": new_article.slug}), 201


@admin_bp.route('/blog/<int:article_id>', methods=['PATCH', 'DELETE'])
@jwt_required()
def manage_blog_article(article_id):
    """Edit or delete a specific blog article by ID."""
    admin_id = get_jwt_identity()
    parsed_id = _parse_admin_id(admin_id)
    if not _require_superadmin(parsed_id):
        return jsonify({"message": "SuperAdmin required"}), 403

    article = db.session.get(Article, article_id)
    if not article:
        return jsonify({"message": "Article not found"}), 404

    if request.method == 'DELETE':
        db.session.delete(article)
        db.session.commit()
        return jsonify({"message": f"Article '{article.title}' deleted."}), 200

    # PATCH — update existing article
    import re

    if request.content_type and 'multipart' in request.content_type:
        data = request.form.to_dict()
        cover_image_file = request.files.get('cover_image')
        if cover_image_file:
            import os, uuid
            from app.utils.upload import save_uploaded_file
            s3_url = save_uploaded_file(cover_image_file, subfolder="blog")
            if s3_url:
                article.cover_image = s3_url
    else:
        data = request.get_json() or {}
        if 'cover_image' in data and isinstance(data['cover_image'], str):
            article.cover_image = data['cover_image']

    if 'title' in data and data['title']:
        article.title = data['title']
        if not data.get('slug'):
            new_slug = re.sub(r'[^a-z0-9]+', '-', data['title'].lower()).strip('-')
            # Ensure new slug is unique
            if new_slug != article.slug:
                original_slug = new_slug
                import uuid
                while Article.query.filter(Article.slug == new_slug, Article.id != article_id).first():
                    new_slug = f"{original_slug}-{str(uuid.uuid4())[:6]}"
                article.slug = new_slug
    if 'slug' in data and data['slug']:
        new_slug = data['slug']
        if new_slug != article.slug:
            original_slug = new_slug
            import uuid
            while Article.query.filter(Article.slug == new_slug, Article.id != article_id).first():
                new_slug = f"{original_slug}-{str(uuid.uuid4())[:6]}"
            article.slug = new_slug
    if 'category' in data:
        article.category = data['category']
    if 'content' in data:
        article.content = data['content']
    if 'excerpt' in data:
        article.excerpt = data['excerpt']
    if 'status' in data:
        article.is_published = data['status'] in ('published', True, 'true', '1')
    if 'is_published' in data:
        article.is_published = data['is_published'] in (True, 'true', '1', 'published')
    if 'meta_title' in data or 'seo_title' in data:
        article.meta_title = data.get('meta_title') or data.get('seo_title')
    if 'meta_description' in data or 'seo_description' in data:
        article.meta_description = data.get('meta_description') or data.get('seo_description')

    db.session.commit()
    return jsonify({"message": "Article updated.", "id": article.id}), 200




# ---------------------------------------------------------------------------
# Platform Settings
# ---------------------------------------------------------------------------

@admin_bp.route('/settings', methods=['GET', 'POST'])
@jwt_required()
def manage_platform_settings():
    admin_id = get_jwt_identity()
    if not _require_superadmin(_parse_admin_id(admin_id)):
        return jsonify({"message": "SuperAdmin required"}), 403

    if request.method == 'GET':
        settings = PlatformSetting.query.all()
        return jsonify([{
            "key": s.key,
            "value": s.value,
            "description": s.description,
        } for s in settings]), 200

    data = request.get_json() or {}
    key = data.get('key')
    value = data.get('value')
    if not key:
        return jsonify({"message": "key is required"}), 400

    setting = PlatformSetting.query.filter_by(key=key).first()
    if not setting:
        setting = PlatformSetting(key=key, value=value, description=data.get('description'))
        db.session.add(setting)
    else:
        setting.value = value
    db.session.commit()
    return jsonify({"message": f"Setting '{key}' updated."}), 200


# ---------------------------------------------------------------------------
# Broadcast Email
# ---------------------------------------------------------------------------

@admin_bp.route('/broadcast', methods=['POST'])
@jwt_required()
def send_email_broadcast():
    admin_id = get_jwt_identity()
    if not _require_superadmin(_parse_admin_id(admin_id)):
        return jsonify({"message": "SuperAdmin required"}), 403

    data = request.get_json() or {}
    target_audience = str(data.get('target_audience') or data.get('audience', 'ALL')).upper()
    subject = data.get('subject', '')
    body = data.get('body', '')
    critical = data.get('critical', False)

    if not subject or not body:
        return jsonify({"message": "Subject and body are required"}), 400

    if target_audience == 'VENDORS':
        recipients = User.query.filter_by(role='VENDOR', is_subscribed_to_broadcasts=True).all()
    elif target_audience == 'BUYERS':
        recipients = User.query.filter_by(role='BUYER', is_subscribed_to_broadcasts=True).all()
    elif target_audience == 'PARTNERS':
        recipients = User.query.filter_by(role='PARTNER', is_subscribed_to_broadcasts=True).all()
    elif target_audience == 'CUSTOM':
        custom_emails_raw = data.get('customEmails') or data.get('custom_emails', '')
        custom_emails = [e.strip() for e in custom_emails_raw.split(',') if e.strip()]
        if not custom_emails:
            return jsonify({"message": "No valid email addresses provided for custom broadcast"}), 400
        class _FakeUser:
            def __init__(self, email):
                self.email = email
                self.first_name = None
                self.is_subscribed_to_broadcasts = True
        recipients = [_FakeUser(e) for e in custom_emails]
    else:
        recipients = User.query.filter_by(is_subscribed_to_broadcasts=True).all()

    sent, failed = 0, 0
    import hashlib
    from flask import current_app
    secret = current_app.config.get('SECRET_KEY', 'default-key')

    for user in recipients:
        try:
            token = hashlib.sha256(f"{user.email}{secret}".encode()).hexdigest()[:16]
            base_url = request.host_url.rstrip('/')
            unsubscribe_link = f"{base_url}/api/auth/unsubscribe?email={user.email}&token={token}"
            
            ok = send_siiqo_email(
                to_email=user.email,
                subject=subject,
                template_name="broadcast",
                first_name=getattr(user, 'first_name', None) or "Siiqo Member",
                body_content=body,
                unsubscribe_link=None if critical else unsubscribe_link
            )
            if ok:
                sent += 1
            else:
                failed += 1
        except Exception as e:
            failed += 1

    return jsonify({
        "message": "Broadcast dispatched.",
        "details": {
            "audience": target_audience,
            "total_recipients": len(recipients),
            "sent": sent,
            "failed": failed,
        },
    }), 200


# ---------------------------------------------------------------------------
# Subscription Management (Admin)
# ---------------------------------------------------------------------------

@admin_bp.route('/subscriptions', methods=['GET'])
@jwt_required()
def list_subscriptions():
    admin = _get_admin(get_jwt_identity())
    if not admin:
        return jsonify({"message": "Admin access required"}), 403

    subs = VendorSubscription.query.all()
    result = []
    for s in subs:
        vendor = db.session.get(User, s.vendor_id)
        plan = db.session.get(SubscriptionPlan, s.plan_id)
        result.append({
            "id": s.id,
            "vendor_id": s.vendor_id,
            "vendor_email": vendor.email if vendor else "N/A",
            "plan_name": plan.name if plan else "N/A",
            "status": s.status,
            "start_date": s.start_date.isoformat() if s.start_date else None,
            "end_date": s.end_date.isoformat() if s.end_date else None,
        })
    return jsonify({"status": "success", "subscriptions": result}), 200


@admin_bp.route('/subscriptions/<int:vendor_id>/grant', methods=['POST'])
@jwt_required()
def grant_subscription(vendor_id):
    """Manually grant a Pro subscription to a vendor."""
    admin = _require_superadmin(get_jwt_identity())
    if not admin:
        return jsonify({"message": "Superadmin access required"}), 403

    user = db.session.get(User, vendor_id)
    if not user:
        return jsonify({"message": "Vendor not found"}), 404

    data = request.get_json() or {}
    months = int(data.get('months', 1))
    plan_name = data.get('plan_name', 'PRO_MONTHLY')

    plan = SubscriptionPlan.query.filter_by(name=plan_name).first()
    if not plan:
        return jsonify({"message": f"Plan '{plan_name}' not found"}), 404

    existing = VendorSubscription.query.filter_by(vendor_id=user.id, status='ACTIVE').all()
    for sub in existing:
        sub.status = 'SUPERSEDED'

    from dateutil.relativedelta import relativedelta
    now = _utcnow()
    new_sub = VendorSubscription(
        vendor_id=user.id,
        plan_id=plan.id,
        status='ACTIVE',
        start_date=now,
        end_date=now + relativedelta(months=months),
    )
    db.session.add(new_sub)
    db.session.commit()

    return jsonify({"status": "success", "message": f"Granted {months} months of {plan_name} to {user.email}"}), 201


@admin_bp.route('/subscriptions/<int:vendor_id>/revoke', methods=['DELETE'])
@jwt_required()
def revoke_subscription(vendor_id):
    """Manually revoke/cancel a vendor's active subscription."""
    admin = _require_superadmin(get_jwt_identity())
    if not admin:
        return jsonify({"message": "Superadmin access required"}), 403

    VendorSubscription.query.filter_by(
        vendor_id=vendor_id, status='ACTIVE'
    ).update({'status': 'CANCELLED'})
    db.session.commit()

    return jsonify({"status": "success", "message": f"Revoked active subscriptions for vendor {vendor_id}"}), 200


@admin_bp.route('/users/<int:user_id>/adjust-balance', methods=['POST'])
@jwt_required()
def adjust_user_balance(user_id):
    """SuperAdmin manual balance adjustments for referrals or promos."""
    admin = _require_superadmin(get_jwt_identity())
    if not admin:
        return jsonify({"message": "Superadmin access required"}), 403

    user = db.session.get(User, user_id)
    if not user:
        return jsonify({"message": "User not found"}), 404

    data = request.get_json() or {}
    amount = float(data.get('amount', 0))
    action = data.get('action', 'CREDIT').strip().upper()
    description = (data.get('description') or '').strip()

    if amount <= 0:
        return jsonify({"message": "Amount must be greater than 0"}), 400

    if action not in ('CREDIT', 'DEBIT'):
        return jsonify({"message": "Action must be CREDIT or DEBIT"}), 400

    from app.models.finance import Ledger
    from app.models.communication import Notification
    from sqlalchemy import func
    import uuid

    # Calculate balance after
    credits = db.session.query(func.sum(Ledger.amount)).filter_by(
        vendor_id=user.id, transaction_type='CREDIT'
    ).scalar() or 0
    debits = db.session.query(func.sum(Ledger.amount)).filter_by(
        vendor_id=user.id, transaction_type='DEBIT'
    ).scalar() or 0

    current_balance = float(credits) - float(debits)
    if action == 'CREDIT':
        balance_after = current_balance + amount
    else:
        balance_after = current_balance - amount

    ledger_entry = Ledger(
        vendor_id=user.id,
        transaction_type=action,
        amount=amount,
        description=description or f"Manual balance adjustment by admin",
        reference_id=f"MANUAL-{uuid.uuid4().hex[:8].upper()}",
        balance_after=balance_after,
    )
    db.session.add(ledger_entry)

    # Notify user
    notification = Notification(
        user_id=user.id,
        title="Wallet Balance Adjusted",
        message=f"Admin has {'credited' if action == 'CREDIT' else 'debited'} your wallet with ₦{amount:,.2f}. Reason: {description or 'Manual balance adjustment.'}",
        type="SYSTEM"
    )
    db.session.add(notification)
    
    db.session.commit()

    return jsonify({
        "status": "success",
        "message": f"Successfully {action}ed ₦{amount:,.2f} to user {user.email}",
        "new_balance": balance_after
    }), 200



# ---------------------------------------------------------------------------
# GET  /admin/payout/vendor-accounts
# POST /admin/payout/fix-bank-code
# POST /admin/payout/retry-order/<order_id>
# POST /admin/payout/retry-all-failed
#
# These routes let admin inspect vendor bank data and retry failed payouts
# without needing direct DB access from CloudShell.
# ---------------------------------------------------------------------------

@admin_bp.route('/payout/vendor-accounts', methods=['GET'])
@jwt_required()
def admin_list_vendor_bank_accounts():
    """
    List all vendor bank accounts — shows bank_code, account_number,
    is_default, is_verified. Use to spot wrong bank codes before they
    cause payout failures.
    """
    from app.models.withdrawal import VendorBankAccount
    admin_id = get_jwt_identity()
    if not _get_admin(_parse_admin_id(admin_id)):
        return jsonify({"message": "Unauthorized"}), 403

    accounts = (
        db.session.query(VendorBankAccount, User)
        .join(User, User.id == VendorBankAccount.vendor_id)
        .order_by(VendorBankAccount.created_at.desc())
        .all()
    )

    return jsonify({
        "accounts": [
            {
                "id": acc.id,
                "vendor_id": acc.vendor_id,
                "vendor_email": user.email,
                "bank_name": acc.bank_name,
                "bank_code": acc.bank_code,
                "account_number": acc.account_number,
                "account_name": acc.account_name,
                "is_default": acc.is_default,
                "is_verified": acc.is_verified,
                "recipient_code": acc.recipient_code,
                "created_at": acc.created_at.isoformat() if acc.created_at else None,
            }
            for acc, user in accounts
        ],
        "count": len(accounts),
    }), 200


@admin_bp.route('/payout/fix-bank-code', methods=['POST'])
@jwt_required()
def admin_fix_bank_code():
    """
    Fix a vendor's stored bank_code and/or bank_name.
    Use when the wrong code is stored (e.g. OPay 999992 → 100004).

    Body: { account_number, bank_code, bank_name (optional) }
    Updates ALL rows matching account_number.
    """
    from app.models.withdrawal import VendorBankAccount
    admin_id = get_jwt_identity()
    if not _require_superadmin(_parse_admin_id(admin_id)):
        return jsonify({"message": "SuperAdmin required"}), 403

    data = request.get_json() or {}
    account_number = (data.get("account_number") or "").strip()
    new_bank_code = (data.get("bank_code") or "").strip()
    new_bank_name = (data.get("bank_name") or "").strip()

    if not account_number or not new_bank_code:
        return jsonify({"message": "account_number and bank_code are required"}), 400

    accounts = VendorBankAccount.query.filter_by(account_number=account_number).all()
    if not accounts:
        return jsonify({"message": f"No bank accounts found with account_number {account_number}"}), 404

    updated = []
    for acc in accounts:
        old_code = acc.bank_code
        old_name = acc.bank_name
        acc.bank_code = new_bank_code
        if new_bank_name:
            acc.bank_name = new_bank_name
        updated.append({
            "id": acc.id,
            "vendor_id": acc.vendor_id,
            "account_number": acc.account_number,
            "old_bank_code": old_code,
            "new_bank_code": new_bank_code,
            "old_bank_name": old_name,
            "new_bank_name": new_bank_name or old_name,
        })

    db.session.commit()
    logging.info("[ADMIN PAYOUT] Fixed bank code for account %s: %s → %s (%d rows)",
                 account_number, accounts[0].bank_code, new_bank_code, len(updated))

    return jsonify({
        "message": f"Bank code updated for {len(updated)} account(s)",
        "updated": updated,
    }), 200


@admin_bp.route('/payout/retry-order/<int:order_id>', methods=['POST'])
@jwt_required()
def admin_retry_payout(order_id):
    """
    Retry the Daya vendor payout for a specific order.
    Use when automatic payout failed (INTEGRATION_FAILED, wrong bank code, etc.)

    The order must be COMPLETED/PAID status with a CRYPTO payment method.
    Funds must already be in Daya withdrawal balance.

    Optional body: { amount_ngn: float } to override the payout amount.
    """
    from app.models.order import Order
    from app.models.escrow import EscrowTransaction
    from app.models.withdrawal import VendorBankAccount
    from app.services import daya_service
    import uuid

    admin_id = get_jwt_identity()
    if not _require_superadmin(_parse_admin_id(admin_id)):
        return jsonify({"message": "SuperAdmin required"}), 403

    order = db.session.get(Order, order_id)
    if not order:
        return jsonify({"message": f"Order #{order_id} not found"}), 404

    escrow = EscrowTransaction.query.filter_by(order_id=order_id).first()
    if not escrow:
        return jsonify({"message": f"No escrow record for Order #{order_id}"}), 404

    data = request.get_json() or {}
    override_amount = data.get("amount_ngn")

    # Calculate payout amount
    if override_amount:
        net_amount_ngn = float(override_amount)
    else:
        net_amount_ngn = float(escrow.amount) - float(escrow.fee_amount or 0)

    # Get vendor's current default bank account
    bank_acc = VendorBankAccount.query.filter_by(
        vendor_id=order.vendor_id, is_default=True
    ).first() or VendorBankAccount.query.filter_by(
        vendor_id=order.vendor_id
    ).first()

    if not bank_acc:
        return jsonify({
            "message": f"Vendor {order.vendor_id} has no bank account registered",
            "vendor_id": order.vendor_id,
        }), 400

    # Step 1: Ensure enough in withdrawal balance
    balance_info = {}
    try:
        balance = daya_service.get_merchant_balance()
        bal_data = balance.get("data", {})
        collection_usd = float(bal_data.get("collection_balance_usd", 0))
        withdrawal_usd = float(bal_data.get("withdrawal_balance_usd", 0))
        balance_info = {"collection_usd": collection_usd, "withdrawal_usd": withdrawal_usd}

        estimated_usd_needed = round((net_amount_ngn / 1380) * 1.02, 4)
        if withdrawal_usd < estimated_usd_needed:
            shortfall = estimated_usd_needed - withdrawal_usd
            amount_to_move = min(round(shortfall + 0.10, 4), collection_usd)
            if amount_to_move > 0:
                idem = f"admin-bal-transfer-{order_id}-{uuid.uuid4().hex[:8]}"
                daya_service.transfer_collection_to_withdrawal(
                    amount_usd=amount_to_move,
                    idempotency_key=idem,
                )
                balance_info["moved_usd"] = amount_to_move
    except Exception as exc:
        balance_info["balance_error"] = str(exc)

    # Step 2: Attempt the NGN transfer with account_name to bypass Daya resolution
    payout_ref = f"ADMIN-RETRY-{order_id}-{uuid.uuid4().hex[:8].upper()}"
    result = daya_service.transfer_ngn_to_vendor(
        amount_ngn=net_amount_ngn,
        bank_code=bank_acc.bank_code,
        account_number=bank_acc.account_number,
        account_name=bank_acc.account_name or "",
        reference=payout_ref,
        order_id=order_id,
    )

    if result.get("success"):
        # Add notification to vendor
        db.session.add(Notification(
            user_id=order.vendor_id,
            title="Payment On Its Way",
            message=(
                f"Order #{order_id} payout has been processed. "
                f"NGN{net_amount_ngn:,.2f} is being transferred to your bank account "
                f"({bank_acc.bank_name} ···{bank_acc.account_number[-4:]})."
            ),
            type="ESCROW",
            order_id=order_id,
        ))
        db.session.commit()
        logging.info("[ADMIN PAYOUT] Retry succeeded for Order #%s: NGN%.2f → %s/%s ref=%s",
                     order_id, net_amount_ngn, bank_acc.bank_code, bank_acc.account_number, payout_ref)
        return jsonify({
            "message": f"Payout initiated successfully for Order #{order_id}",
            "amount_ngn": net_amount_ngn,
            "bank_code": bank_acc.bank_code,
            "account_number": bank_acc.account_number,
            "account_name": bank_acc.account_name,
            "reference": payout_ref,
            "balance_info": balance_info,
        }), 200
    else:
        logging.error("[ADMIN PAYOUT] Retry FAILED for Order #%s: %s",
                      order_id, result.get("error_message"))
        return jsonify({
            "message": f"Payout failed for Order #{order_id}",
            "error": result.get("error_message"),
            "bank_code_used": bank_acc.bank_code,
            "account_number_used": bank_acc.account_number,
            "account_name_used": bank_acc.account_name,
            "reference": payout_ref,
            "balance_info": balance_info,
            "hint": "If error is INTEGRATION_FAILED, the bank_code may still be wrong. "
                    "Use POST /admin/payout/fix-bank-code first, then retry.",
        }), 502


@admin_bp.route('/payout/retry-all-failed', methods=['POST'])
@jwt_required()
def admin_retry_all_failed_payouts():
    """
    Scan all COMPLETED crypto orders where vendor hasn't been paid
    (escrow is RELEASED but no successful Daya transfer logged) and retry each.

    This is the 'fix it once and for all' route — run it after fixing bank codes
    and it will sweep through every unpaid order automatically.
    """
    from app.models.order import Order
    from app.models.escrow import EscrowTransaction
    from app.models.withdrawal import VendorBankAccount, DayaPayment
    from app.services import daya_service
    import uuid

    admin_id = get_jwt_identity()
    if not _require_superadmin(_parse_admin_id(admin_id)):
        return jsonify({"message": "SuperAdmin required"}), 403

    # Find all crypto orders that are COMPLETED/PAID but whose escrow is
    # RELEASED (meaning we already confirmed payment) — these are candidates
    # for a vendor payout retry.
    # We use a dry-run flag to preview without actually sending.
    dry_run = request.get_json().get("dry_run", False) if request.get_json() else False

    completed_crypto_orders = (
        db.session.query(Order, EscrowTransaction)
        .join(EscrowTransaction, EscrowTransaction.order_id == Order.id)
        .filter(
            Order.payment_method == "CRYPTO",
            Order.status.in_(["COMPLETED", "PAID"]),
            EscrowTransaction.status == EscrowStatus.RELEASED,
        )
        .all()
    )

    results = []
    for order, escrow in completed_crypto_orders:
        bank_acc = VendorBankAccount.query.filter_by(
            vendor_id=order.vendor_id, is_default=True
        ).first() or VendorBankAccount.query.filter_by(
            vendor_id=order.vendor_id
        ).first()

        net_amount_ngn = float(escrow.amount) - float(escrow.fee_amount or 0)

        entry = {
            "order_id": order.id,
            "vendor_id": order.vendor_id,
            "amount_ngn": net_amount_ngn,
            "bank_code": bank_acc.bank_code if bank_acc else None,
            "account_number": bank_acc.account_number if bank_acc else None,
            "account_name": bank_acc.account_name if bank_acc else None,
            "bank_name": bank_acc.bank_name if bank_acc else None,
            "status": None,
            "error": None,
            "reference": None,
        }

        if not bank_acc:
            entry["status"] = "SKIPPED"
            entry["error"] = "No bank account registered for vendor"
            results.append(entry)
            continue

        if dry_run:
            entry["status"] = "DRY_RUN"
            results.append(entry)
            continue

        # Attempt payout
        payout_ref = f"ADMIN-SWEEP-{order.id}-{uuid.uuid4().hex[:8].upper()}"
        entry["reference"] = payout_ref

        try:
            result = daya_service.transfer_ngn_to_vendor(
                amount_ngn=net_amount_ngn,
                bank_code=bank_acc.bank_code,
                account_number=bank_acc.account_number,
                account_name=bank_acc.account_name or "",
                reference=payout_ref,
                order_id=order.id,
            )
            if result.get("success"):
                entry["status"] = "SUCCESS"
                db.session.add(Notification(
                    user_id=order.vendor_id,
                    title="Payment On Its Way",
                    message=(
                        f"Order #{order.id} payout processed. "
                        f"NGN{net_amount_ngn:,.2f} is being transferred to your bank account."
                    ),
                    type="ESCROW",
                    order_id=order.id,
                ))
            else:
                entry["status"] = "FAILED"
                entry["error"] = result.get("error_message")
        except Exception as exc:
            entry["status"] = "ERROR"
            entry["error"] = str(exc)

        results.append(entry)

    if not dry_run:
        db.session.commit()

    success_count = sum(1 for r in results if r["status"] == "SUCCESS")
    failed_count = sum(1 for r in results if r["status"] in ("FAILED", "ERROR"))
    skipped_count = sum(1 for r in results if r["status"] == "SKIPPED")

    return jsonify({
        "dry_run": dry_run,
        "total_orders_checked": len(results),
        "success": success_count,
        "failed": failed_count,
        "skipped": skipped_count,
        "results": results,
    }), 200


# ---------------------------------------------------------------------------
# POST /admin/data/wipe-transactions
#
# Wipes all test transaction data — orders, escrow, daya_payments, ledger,
# receipts, notifications linked to orders — while preserving users,
# products, storefronts, and bank accounts.
#
# SUPERADMIN only. Requires { "confirm": "WIPE_ALL_TRANSACTIONS" } in body
# as a safety check so it can't be triggered accidentally.
# ---------------------------------------------------------------------------

@admin_bp.route('/data/wipe-transactions', methods=['POST'])
@jwt_required()
def admin_wipe_transactions():
    """
    Wipe all test transaction data cleanly.
    Preserves: users, products, storefronts, bank accounts, blog, categories.
    Deletes: orders, order_items, escrow_transactions, daya_payments,
             ledger entries, receipts, order-linked notifications.

    Body must include: { "confirm": "WIPE_ALL_TRANSACTIONS" }
    """
    admin_id = get_jwt_identity()
    if not _require_superadmin(_parse_admin_id(admin_id)):
        return jsonify({"message": "SuperAdmin required"}), 403

    data = request.get_json() or {}
    if data.get("confirm") != "WIPE_ALL_TRANSACTIONS":
        return jsonify({
            "message": "Safety check failed. Send { \"confirm\": \"WIPE_ALL_TRANSACTIONS\" } to proceed.",
            "hint": "This deletes ALL orders, escrow records, daya payments, ledger entries and notifications.",
        }), 400

    from app.models.order import Order, OrderItem
    from app.models.escrow import EscrowTransaction
    from app.models.withdrawal import DayaPayment
    from app.models.finance import Ledger, Receipt
    from app.models.communication import Notification
    from sqlalchemy import text

    counts = {}

    try:
        # Delete in dependency order — children before parents to avoid FK violations

        # 1. Notifications and messages linked to orders
        notif_result = db.session.execute(
            text("DELETE FROM notifications WHERE order_id IS NOT NULL")
        )
        counts["notifications"] = notif_result.rowcount

        msg_result = db.session.execute(
            text("DELETE FROM messages WHERE order_id IS NOT NULL")
        )
        counts["messages"] = msg_result.rowcount

        # 2. Ledger entries
        ledger_result = db.session.execute(text("DELETE FROM ledgers"))
        counts["ledger_entries"] = ledger_result.rowcount

        # 3. Invoices (references orders)
        invoice_result = db.session.execute(text("DELETE FROM invoices"))
        counts["invoices"] = invoice_result.rowcount

        # 4. Receipts (references orders)
        receipt_result = db.session.execute(text("DELETE FROM receipts"))
        counts["receipts"] = receipt_result.rowcount

        # 5. Reviews (references orders)
        try:
            review_result = db.session.execute(
                text("DELETE FROM reviews WHERE order_id IS NOT NULL")
            )
            counts["reviews"] = review_result.rowcount
        except Exception:
            counts["reviews"] = 0  # table may not exist yet

        # 6. Daya payments (references orders)
        daya_result = db.session.execute(text("DELETE FROM daya_payments"))
        counts["daya_payments"] = daya_result.rowcount

        # 7. POD payments (references orders)
        try:
            pod_result = db.session.execute(text("DELETE FROM pod_payments"))
            counts["pod_payments"] = pod_result.rowcount
        except Exception:
            counts["pod_payments"] = 0

        # 8. Logistics assignments (references orders)
        try:
            la_result = db.session.execute(text("DELETE FROM logistics_assignments"))
            counts["logistics_assignments"] = la_result.rowcount
        except Exception:
            counts["logistics_assignments"] = 0

        # 9. Escrow transactions (references orders)
        escrow_result = db.session.execute(text("DELETE FROM escrow_transactions"))
        counts["escrow_transactions"] = escrow_result.rowcount

        # 10. Order items (references orders)
        items_result = db.session.execute(text("DELETE FROM order_items"))
        counts["order_items"] = items_result.rowcount

        # 11. Orders — everything that references them is now gone
        orders_result = db.session.execute(text("DELETE FROM orders"))
        counts["orders"] = orders_result.rowcount

        db.session.commit()

        logging.warning(
            "[ADMIN WIPE] SuperAdmin %s wiped all transactions. Counts: %s",
            admin_id, counts
        )

        return jsonify({
            "message": "All transaction data wiped successfully. Ready for fresh testing.",
            "deleted": counts,
            "preserved": [
                "users", "products", "storefronts", "vendor_bank_accounts",
                "vendor_crypto_wallets", "categories", "blog_articles",
                "cart_items", "platform_settings",
            ],
        }), 200

    except Exception as exc:
        db.session.rollback()
        logging.error("[ADMIN WIPE] Failed: %s", exc)
        return jsonify({"message": f"Wipe failed: {str(exc)}"}), 500


# ---------------------------------------------------------------------------
# POST /admin/storefronts/<int:storefront_id>/verify-pro
# ---------------------------------------------------------------------------

@admin_bp.route('/storefronts/<int:storefront_id>/verify-pro', methods=['POST'])
@jwt_required()
def admin_verify_pro_storefront(storefront_id):
    admin_id = get_jwt_identity()
    admin = _get_admin(admin_id)
    if not admin:
        return jsonify({"message": "Admin authorization required"}), 403

    sf = db.session.get(Storefront, storefront_id)
    if not sf:
        return jsonify({"message": "Storefront not found"}), 404

    from datetime import timedelta
    sf.is_pro_verified = True
    sf.pro_verified_expires_at = _utcnow() + timedelta(days=365)
    sf.verification_status = 'APPROVED'
    sf.is_verified = True

    db.session.add(Notification(
        user_id=sf.vendor_id,
        title="Pro Verified Status Activated! ⭐",
        message="Congratulations! Your NIN & CAC verification has been approved. You now enjoy Pro Verified Gold status and a reduced 5.4% platform fee.",
        type="SYSTEM"
    ))

    db.session.commit()
    return jsonify({
        "status": "success",
        "message": f"Storefront #{sf.id} '{sf.store_name}' is now Pro Verified until {sf.pro_verified_expires_at.strftime('%Y-%m-%d')}.",
        "storefront": sf.to_public_dict()
    }), 200


# ---------------------------------------------------------------------------
# POST /admin/storefronts/publish-all-approved
# One-time action: publish + grant Pro Verified to all admin-approved storefronts.
# Use this to activate all founding vendors at once.
# ---------------------------------------------------------------------------

@admin_bp.route('/storefronts/publish-all-approved', methods=['POST'])
@jwt_required()
def publish_all_approved_storefronts():
    """
    Bulk-activates all admin-approved storefronts that are not yet published.
    For each storefront where is_verified=True:
      - Sets is_published = True (makes store live on marketplace)
      - Sets is_pro_verified = True (grants Pro Verified badge)
      - Sets pro_verified_expires_at = now + 1 year
      - Sends a founder notification email + in-app notification

    Safe to call multiple times — already-published stores are skipped.
    SuperAdmin only.
    """
    admin_id = get_jwt_identity()
    if not _require_superadmin(_parse_admin_id(admin_id)):
        return jsonify({"message": "SuperAdmin required"}), 403

    from datetime import datetime as _dt, timedelta as _td, timezone as _tz
    from app.models.communication import Notification as _Notif
    from app.utils.email import send_siiqo_email

    now = _dt.now(_tz.utc)
    pro_expiry = now + _td(days=365)

    # Find all admin-approved storefronts
    approved = Storefront.query.filter_by(is_verified=True).all()

    published_count = 0
    already_live_count = 0
    email_sent_count = 0
    errors = []

    for sf in approved:
        vendor = sf.vendor
        if not vendor:
            continue

        was_already_live = sf.is_published

        # Publish the store
        sf.is_published = True

        # Grant Pro Verified if not already active
        if not sf.is_pro_verified or not sf.pro_verified_expires_at or sf.pro_verified_expires_at < now:
            sf.is_pro_verified = True
            sf.pro_verified_expires_at = pro_expiry

        if not was_already_live:
            published_count += 1
            # Send in-app notification
            db.session.add(_Notif(
                user_id=vendor.id,
                title="🎉 Your store is now LIVE!",
                message=(
                    f"Congratulations, {vendor.first_name or 'Vendor'}! "
                    f"Your store '{sf.store_name}' is now live on Siiqo. "
                    "As a founding vendor, you also have Pro Verified access for 1 year — at no charge."
                ),
                type="ACCOUNT",
            ))
            # Send email
            try:
                store_url = f"https://siiqo.com/{sf.store_slug}"
                send_siiqo_email(
                    to_email=vendor.email,
                    subject="🎉 Your Siiqo Store Is Now Live — Founding Vendor Access",
                    template_name="founder_store_live",
                    first_name=vendor.first_name or "Vendor",
                    store_name=sf.store_name,
                    store_url=store_url,
                )
                email_sent_count += 1
            except Exception as e:
                errors.append(f"Email failed for {vendor.email}: {str(e)}")
                logging.warning(f"[BULK PUBLISH] Email failed for storefront #{sf.id}: {e}")
        else:
            already_live_count += 1
            # Still grant Pro Verified if they didn't have it
            if not was_already_live:
                pass  # notification already sent above
            else:
                # Grant Pro Verified silently if missing
                pass

    try:
        db.session.commit()
    except Exception as e:
        db.session.rollback()
        return jsonify({"message": f"Database error: {str(e)}"}), 500

    logging.info(
        f"[BULK PUBLISH] Admin {admin_id} activated founders: "
        f"published={published_count}, already_live={already_live_count}, "
        f"emails_sent={email_sent_count}, errors={len(errors)}"
    )

    return jsonify({
        "status": "success",
        "summary": {
            "total_approved_storefronts": len(approved),
            "newly_published": published_count,
            "already_live": already_live_count,
            "emails_sent": email_sent_count,
            "errors": errors,
        },
        "message": (
            f"Done. {published_count} store(s) newly published and notified. "
            f"{already_live_count} were already live. "
            f"All approved vendors now have Pro Verified access for 1 year."
        ),
    }), 200
