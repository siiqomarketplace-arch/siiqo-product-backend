"""
vendor.py — Vendor-facing routes
Handles: onboarding, storefront settings, products, orders, finance, CRM, marketing
"""
import json
import re
import uuid as _uuid

from flask import Blueprint, request, jsonify
from flask_jwt_extended import jwt_required, get_jwt_identity

from app.extensions import db
from app.models.user import User, Storefront, UserRole
from app.models.product import Product, Catalog, Category
from app.models.order import Order
from app.models.finance import Invoice, Ledger
from app.models.marketing import Coupon, Campaign
from app.models.crm import CustomerProfile
from app.utils.upload import save_uploaded_file
from app.utils.email import send_siiqo_email

vendor_bp = Blueprint('vendor', __name__)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _get_vendor(user_id) -> User | None:
    user = db.session.get(User, int(user_id))
    if not user or user.role not in [UserRole.VENDOR, UserRole.ADMIN]:
        return None
    return user


def _require_vendor_storefront(user_id):
    """Returns (user, storefront) or raises a 403 response tuple."""
    user = db.session.get(User, int(user_id))
    if not user or user.role not in [UserRole.VENDOR, UserRole.ADMIN]:
        return None, None
    if not user.storefront:
        return user, None
    return user, user.storefront


# ---------------------------------------------------------------------------
# GET /vendor/settings  — unified profile for all users
# ---------------------------------------------------------------------------

@vendor_bp.route('/settings', methods=['GET'])
@jwt_required()
def get_settings():
    user_id = get_jwt_identity()
    user = db.session.get(User, int(user_id))
    if not user:
        return jsonify({"message": "User not found"}), 404

    sf = user.storefront
    full_name = user.full_name

    if sf:
        store_settings = {
            "initialized": True,
            "business_name": sf.store_name,
            "store_slug": sf.store_slug,
            "storefront_link": sf.store_slug,
            "description": sf.store_description or "",
            "address": sf.address or "",
            "city": sf.city,
            "state": sf.state,
            "logo_url": sf.store_logo or user.profile_pic,
            "banner_url": sf.banner_url,
            "theme_color": sf.theme_color or "#0b1b3b",
            "is_published": sf.is_published,
            "is_verified": sf.is_verified,
            "is_live": sf.is_live,
            "bank_code": sf.bank_code,
            "account_number": sf.account_number,
            "account_name": sf.account_name,
            "phone": sf.phone,
            "website": sf.website,
            "cac_reg": sf.cac_reg,
            "template_options": sf.template_options or {},
            "social_links": sf.social_links or {},
            "working_hours": sf.working_hours or {},
            "meta_title": sf.meta_title,
            "meta_description": sf.meta_description,
        }
    else:
        store_settings = {
            "initialized": False,
            "business_name": "",
            "store_slug": None,
            "description": "",
            "address": "",
            "city": None,
            "state": None,
            "logo_url": user.profile_pic,
            "banner_url": None,
            "theme_color": "#0b1b3b",
            "is_published": False,
            "is_verified": False,
            "is_live": False,
            "bank_code": None,
            "account_number": None,
            "account_name": None,
            "phone": None,
            "website": None,
            "cac_reg": None,
            "template_options": {},
            "social_links": {},
            "working_hours": {},
            "meta_title": None,
            "meta_description": None,
        }

    return jsonify({
        "id": user.id,
        "email": user.email,
        "role": user.role,
        "first_name": user.first_name,
        "last_name": user.last_name,
        "phone": user.phone,
        "profile_pic": user.profile_pic,
        "is_verified": user.is_verified,
        "personal_info": {
            "fullname": full_name,
            "email": user.email,
            "phone": user.phone or "",
        },
        "store_settings": store_settings,
    }), 200


# ---------------------------------------------------------------------------
# POST /vendor/onboard
# ---------------------------------------------------------------------------

@vendor_bp.route('/onboard', methods=['POST'])
@jwt_required()
def onboard_vendor():
    user_id = get_jwt_identity()
    user = db.session.get(User, int(user_id))
    if not user:
        return jsonify({"message": "User not found"}), 404

    if not user.is_verified:
        return jsonify({"message": "Please verify your email before becoming a vendor."}), 403

    if user.storefront:
        return jsonify({
            "message": "You already have a storefront.",
            "storefront_id": user.storefront.id,
            "store_slug": user.storefront.store_slug,
        }), 200

    data = request.form if request.form else (request.get_json() or {})

    store_name = (data.get('business_name') or data.get('store_name') or '').strip()
    if not store_name:
        return jsonify({"message": "Store name is required"}), 400

    # Auto-generate unique slug
    base_slug = re.sub(r'[^a-z0-9]+', '-', store_name.lower()).strip('-')
    store_slug = base_slug
    if Storefront.query.filter_by(store_slug=store_slug).first():
        store_slug = f"{base_slug}-{_uuid.uuid4().hex[:6]}"

    user.role = UserRole.VENDOR

    storefront = Storefront(
        vendor_id=user.id,
        store_name=store_name,
        store_slug=store_slug,
        store_description=data.get('description') or data.get('store_description'),
        address=data.get('address'),
        city=data.get('city'),
        state=data.get('state'),
        country=data.get('country', 'Nigeria'),
        bank_code=data.get('bank_code') or data.get('bank_name'),
        account_number=data.get('account_number'),
        account_name=data.get('account_name'),
        phone=data.get('phone'),
        website=data.get('website'),
        cac_reg=data.get('cac_reg'),
    )

    logo_file = request.files.get('logo') or request.files.get('store_logo')
    banner_file = request.files.get('banner') or request.files.get('banner_url')

    if logo_file:
        try:
            storefront.store_logo = save_uploaded_file(logo_file, subfolder='storefronts')
        except ValueError as e:
            return jsonify({"message": str(e)}), 400

    if banner_file:
        try:
            storefront.banner_url = save_uploaded_file(banner_file, subfolder='storefronts')
        except ValueError as e:
            return jsonify({"message": str(e)}), 400

    db.session.add(storefront)
    db.session.commit()

    try:
        send_siiqo_email(
            to_email=user.email,
            subject="Vendor Application Received — Siiqo",
            template_name="vendor_onboarding_submitted",
            first_name=user.first_name or "Vendor",
            store_name=storefront.store_name,
        )
    except Exception:
        pass

    return jsonify({
        "message": "Vendor onboarding complete. Your application is under review.",
        "store_name": storefront.store_name,
        "store_slug": storefront.store_slug,
        "status": "success",
    }), 201


# ---------------------------------------------------------------------------
# PATCH /vendor/update-settings
# ---------------------------------------------------------------------------

@vendor_bp.route('/update-settings', methods=['PATCH'])
@jwt_required()
def update_settings():
    user_id = get_jwt_identity()
    user, sf = _require_vendor_storefront(user_id)
    if not user:
        return jsonify({"message": "Vendor access required"}), 403
    if not sf:
        return jsonify({"message": "Complete vendor onboarding first"}), 403

    data = request.form if request.form else (request.get_json() or {})

    # Scalar fields
    field_map = {
        'store_description': 'store_description',
        'description': 'store_description',
        'business_name': 'store_name',
        'address': 'address',
        'city': 'city',
        'state': 'state',
        'bank_code': 'bank_code',
        'account_number': 'account_number',
        'account_name': 'account_name',
        'theme_color': 'theme_color',
        'phone': 'phone',
        'website': 'website',
        'cac_reg': 'cac_reg',
        'meta_title': 'meta_title',
        'meta_description': 'meta_description',
    }
    for form_key, model_attr in field_map.items():
        if form_key in data:
            setattr(sf, model_attr, data[form_key])

    # Publish flag — vendor can set to True, but it only goes live after admin approval
    if 'is_published' in data:
        sf.is_published = str(data['is_published']).lower() in ('true', '1', 'yes')

    # JSON fields
    for json_field in ('template_options', 'social_links', 'working_hours'):
        if json_field in data:
            val = data[json_field]
            if isinstance(val, str):
                try:
                    val = json.loads(val)
                except Exception:
                    pass
            setattr(sf, json_field, val)

    # File uploads
    if 'store_logo' in request.files:
        try:
            sf.store_logo = save_uploaded_file(request.files['store_logo'], subfolder='storefronts')
        except ValueError as e:
            return jsonify({"message": str(e)}), 400

    if 'banner_url' in request.files:
        try:
            sf.banner_url = save_uploaded_file(request.files['banner_url'], subfolder='storefronts')
        except ValueError as e:
            return jsonify({"message": str(e)}), 400

    db.session.commit()

    pending_approval = sf.is_published and not sf.is_verified
    return jsonify({
        "status": "success",
        "message": "Settings updated successfully",
        "is_published": sf.is_published,
        "is_verified": sf.is_verified,
        "is_live": sf.is_live,
        "pending_approval": pending_approval,
        "notice": (
            "Your storefront is saved and set to publish. "
            "It will go live once our team approves your application."
            if pending_approval else None
        ),
    }), 200


# ---------------------------------------------------------------------------
# Products
# ---------------------------------------------------------------------------

@vendor_bp.route('/products/add', methods=['POST'])
@jwt_required()
def add_product():
    user_id = get_jwt_identity()
    user, sf = _require_vendor_storefront(user_id)
    if not user:
        return jsonify({"message": "Vendor access required"}), 403
    if not sf:
        return jsonify({"message": "Complete vendor onboarding first"}), 403

    data = request.form if request.form else (request.get_json() or {})

    name = (data.get('name') or '').strip()
    if not name:
        return jsonify({"message": "Product name is required"}), 400

    try:
        price = float(data.get('price') or data.get('price-text') or 0)
    except (ValueError, TypeError):
        return jsonify({"message": "Invalid price"}), 400

    new_product = Product(
        storefront_id=sf.id,
        name=name,
        description=data.get('description', ''),
        price=price,
        stock_quantity=int(data.get('stock_quantity', 1)),
        category_id=data.get('category_id') or None,
    )

    image_file = request.files.get('image') or request.files.get('images-file')
    if image_file:
        try:
            saved_url = save_uploaded_file(image_file, subfolder='products')
            new_product.images = [saved_url]
        except ValueError as e:
            return jsonify({"message": str(e)}), 400

    db.session.add(new_product)
    db.session.flush()  # get new_product.id before commit

    # Auto-post to community feed as PRODUCT_LAUNCH
    # Only if vendor opted in (announce_to_community param) or always for new products
    announce = str(data.get('announce_to_community', 'true')).lower() not in ('false', '0', 'no')
    if announce:
        try:
            from app.models.social import Post as CommunityPost
            store_name = sf.store_name or user.full_name or 'A vendor'
            desc = new_product.description or ''
            desc_snippet = (desc[:150] + '...') if len(desc) > 150 else desc
            post_content = f"🚀 New product just dropped!\n\n{name} — ₦{price:,.0f}"
            if desc_snippet:
                post_content += f"\n\n{desc_snippet}"
            post_content += f"\n\nFrom {store_name} — visit our storefront to order!"

            community_post = CommunityPost(
                user_id=user_id,
                post_type='PRODUCT_LAUNCH',
                content=post_content,
                images=new_product.images or [],
                city=user.city,
                state=user.state,
            )
            db.session.add(community_post)
        except Exception as e:
            # Non-fatal — don't block product creation if community post fails
            import logging
            logging.warning(f"Auto community post failed for product {new_product.id}: {e}")

    db.session.commit()
    return jsonify({
        "message": "Product added successfully",
        "id": new_product.id,
        "status": "success",
    }), 201


@vendor_bp.route('/products/update/<int:product_id>', methods=['PATCH'])
@jwt_required()
def edit_product(product_id):
    user_id = get_jwt_identity()
    user, sf = _require_vendor_storefront(user_id)
    if not user:
        return jsonify({"message": "Vendor access required"}), 403

    product = db.session.get(Product, product_id)
    if not product:
        return jsonify({"message": "Product not found"}), 404
    if sf and product.storefront_id != sf.id:
        return jsonify({"message": "Unauthorized"}), 403

    data = request.form if request.form else (request.get_json() or {})

    if 'name' in data:
        product.name = data['name']
    if 'description' in data:
        product.description = data['description']
    if 'price' in data or 'price-text' in data:
        try:
            product.price = float(data.get('price') or data.get('price-text'))
        except (ValueError, TypeError):
            pass
    if 'stock_quantity' in data:
        product.stock_quantity = int(data['stock_quantity'])
    if 'is_active' in data:
        product.is_active = str(data['is_active']).lower() in ('true', '1', 'yes')
    if 'category_id' in data:
        product.category_id = data['category_id'] or None

    image_file = request.files.get('image') or request.files.get('images-file')
    if image_file:
        try:
            saved_url = save_uploaded_file(image_file, subfolder='products')
            product.images = list(product.images or []) + [saved_url]
        except ValueError as e:
            return jsonify({"message": str(e)}), 400

    db.session.commit()
    return jsonify({"message": "Product updated successfully", "status": "success"}), 200


@vendor_bp.route('/products/my-products', methods=['GET'])
@jwt_required()
def my_products():
    user_id = get_jwt_identity()
    user, sf = _require_vendor_storefront(user_id)
    if not user or not sf:
        return jsonify([]), 200

    products = Product.query.filter_by(storefront_id=sf.id).all()
    return jsonify([{
        "id": p.id,
        "name": p.name,
        "price": str(p.price),
        "stock": p.stock_quantity,
        "is_active": p.is_active,
        "images": p.images or [],
        "description": p.description,
        "category_id": p.category_id,
        "created_at": p.created_at.isoformat() if p.created_at else None,
    } for p in products]), 200


# ---------------------------------------------------------------------------
# Orders
# ---------------------------------------------------------------------------

@vendor_bp.route('/orders', methods=['GET'])
@jwt_required()
def get_orders():
    user_id = get_jwt_identity()
    user = _get_vendor(user_id)
    if not user:
        return jsonify({"message": "Vendor access required"}), 403

    orders = Order.query.filter_by(vendor_id=user.id).order_by(Order.created_at.desc()).all()
    return jsonify([{
        "id": o.id,
        "total_amount": str(o.total_amount),
        "status": o.status,
        "created_at": o.created_at.isoformat() if o.created_at else None,
        "buyer": {
            "name": o.buyer.full_name if o.buyer else "Unknown",
            "email": o.buyer.email if o.buyer else "",
            "phone": o.buyer.phone if o.buyer else "",
        },
        "items": [{
            "product_id": item.product_id,
            "name": item.product.name if item.product else "Unknown",
            "quantity": item.quantity,
            "price": str(item.price_at_purchase),
        } for item in o.items],
    } for o in orders]), 200


@vendor_bp.route('/orders/<int:order_id>/status', methods=['PUT', 'PATCH'])
@jwt_required()
def update_order_status(order_id):
    user_id = get_jwt_identity()
    user = _get_vendor(user_id)
    if not user:
        return jsonify({"message": "Vendor access required"}), 403

    order = db.session.get(Order, order_id)
    if not order:
        return jsonify({"message": "Order not found"}), 404
    if order.vendor_id != user.id:
        return jsonify({"message": "Unauthorized"}), 403

    new_status = (request.get_json() or {}).get('status')
    if not new_status:
        return jsonify({"message": "Status is required"}), 400

    order.status = new_status
    db.session.commit()
    return jsonify({"message": f"Order status updated to {new_status}", "status": "success"}), 200


# ---------------------------------------------------------------------------
# Finance — Ledger
# ---------------------------------------------------------------------------

@vendor_bp.route('/finance/ledger', methods=['GET'])
@jwt_required()
def get_ledger():
    user_id = get_jwt_identity()
    user = _get_vendor(user_id)
    if not user:
        return jsonify({"message": "Vendor access required"}), 403

    records = Ledger.query.filter_by(vendor_id=user.id).order_by(Ledger.created_at.desc()).all()

    # Summary
    total_credits = sum(float(r.amount) for r in records if r.transaction_type == 'CREDIT')
    total_debits = sum(float(r.amount) for r in records if r.transaction_type == 'DEBIT')

    return jsonify({
        "summary": {
            "total_credits": total_credits,
            "total_debits": total_debits,
            "net_balance": total_credits - total_debits,
            "currency": "NGN",
        },
        "entries": [{
            "id": r.id,
            "transaction_type": r.transaction_type,
            "amount": str(r.amount),
            "description": r.description,
            "reference_id": r.reference_id,
            "balance_after": str(r.balance_after) if r.balance_after else None,
            "created_at": r.created_at.isoformat() if r.created_at else None,
        } for r in records],
    }), 200


# ---------------------------------------------------------------------------
# CRM — Customers
# ---------------------------------------------------------------------------

@vendor_bp.route('/crm/customers', methods=['GET'])
@jwt_required()
def get_customers():
    user_id = get_jwt_identity()
    user = _get_vendor(user_id)
    if not user:
        return jsonify({"message": "Vendor access required"}), 403

    profiles = CustomerProfile.query.filter_by(vendor_id=user.id).all()
    return jsonify({
        "status": "success",
        "customers": [{
            "buyer_id": p.buyer_id,
            "name": p.buyer.full_name if p.buyer else "Unknown",
            "email": p.buyer.email if p.buyer else "",
            "total_spent": str(p.total_spent),
            "total_orders": p.total_orders,
            "segment": p.segment,
            "last_purchase_date": p.last_purchase_date.isoformat() if p.last_purchase_date else None,
            "tags": p.tags if hasattr(p, 'tags') else [],
        } for p in profiles],
    }), 200


# ---------------------------------------------------------------------------
# Marketing — Coupons
# ---------------------------------------------------------------------------

@vendor_bp.route('/marketing/coupons', methods=['GET', 'POST'])
@jwt_required()
def handle_coupons():
    user_id = get_jwt_identity()
    user = _get_vendor(user_id)
    if not user:
        return jsonify({"message": "Vendor access required"}), 403

    if request.method == 'GET':
        coupons = Coupon.query.filter_by(vendor_id=user.id).all()
        return jsonify({
            "status": "success",
            "coupons": [{
                "id": c.id,
                "code": c.code,
                "discount_type": c.discount_type,
                "discount_value": str(c.discount_value),
                "usage_limit": c.usage_limit,
                "times_used": c.times_used,
                "is_active": c.is_active,
                "valid_until": c.valid_until.isoformat() if c.valid_until else None,
            } for c in coupons],
        }), 200

    data = request.get_json() or {}
    code = (data.get('code') or '').strip().upper()
    if not code:
        return jsonify({"message": "Coupon code is required"}), 400

    if Coupon.query.filter_by(code=code).first():
        return jsonify({"message": "Coupon code already exists"}), 409

    try:
        discount_value = float(data.get('discount_value') or data.get('discount_percentage') or 0)
    except (ValueError, TypeError):
        return jsonify({"message": "Invalid discount value"}), 400

    new_coupon = Coupon(
        vendor_id=user.id,
        code=code,
        discount_type=data.get('discount_type', 'PERCENTAGE'),
        discount_value=discount_value,
        usage_limit=data.get('usage_limit') or data.get('max_uses') or None,
        is_active=True,
    )
    db.session.add(new_coupon)
    db.session.commit()
    return jsonify({
        "message": f"Coupon '{code}' created successfully.",
        "id": new_coupon.id,
        "status": "success",
    }), 201


# ---------------------------------------------------------------------------
# Marketing — Campaigns
# ---------------------------------------------------------------------------

@vendor_bp.route('/marketing/campaigns', methods=['GET', 'POST'])
@jwt_required()
def handle_campaigns():
    user_id = get_jwt_identity()
    user = _get_vendor(user_id)
    if not user:
        return jsonify({"message": "Vendor access required"}), 403

    if request.method == 'GET':
        campaigns = Campaign.query.filter_by(vendor_id=user.id).all()
        return jsonify({
            "status": "success",
            "campaigns": [{
                "id": c.id,
                "name": c.name,
                "status": c.status,
                "target_segment": c.target_segment,
                "sent_count": c.sent_count,
                "created_at": c.created_at.isoformat() if c.created_at else None,
            } for c in campaigns],
        }), 200

    data = request.get_json() or {}
    campaign = Campaign(
        vendor_id=user.id,
        name=data.get('name', 'Untitled Campaign'),
        target_segment=data.get('target_segment', 'ALL'),
        subject=data.get('subject'),
        body=data.get('body'),
        status='DRAFT',
    )
    db.session.add(campaign)
    db.session.commit()
    return jsonify({
        "message": "Campaign created",
        "id": campaign.id,
        "status": "success",
    }), 201
