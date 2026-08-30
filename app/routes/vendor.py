"""
vendor.py — Vendor-facing routes
Handles: onboarding, storefront settings, products, orders, finance, CRM, marketing
"""
import json
import logging
import re
import uuid as _uuid

from flask import Blueprint, request, jsonify
from flask_jwt_extended import jwt_required, get_jwt_identity
from sqlalchemy.orm import joinedload

from app.extensions import db
from app.models.user import User, Storefront, UserRole
from app.models.product import Product, Catalog, Category
from app.models.order import Order
from app.models.finance import Invoice, Ledger
from app.models.marketing import Coupon, Campaign
from app.models.crm import CustomerProfile
from app.utils.upload import save_uploaded_file
from app.utils.email import send_siiqo_email
from app.utils.algolia_sync import sync_product_to_algolia, delete_product_from_algolia
from app.utils.scraper import scrape_product_url, analyze_storefront_url

vendor_bp = Blueprint('vendor', __name__)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _get_vendor(user_id) -> User | None:
    user = db.session.get(User, int(user_id))
    if not user:
        return None
    # Primary check: role-based access
    if user.role in [UserRole.VENDOR, UserRole.ADMIN]:
        return user
    # Fallback: if user has a storefront but role was never updated, heal and allow
    if user.storefront is not None:
        user.role = UserRole.VENDOR
        try:
            db.session.commit()
        except Exception:
            db.session.rollback()
        return user
    return None


def _require_vendor_storefront(user_id):
    """Returns (user, storefront) or (None, None) if not a vendor."""
    user = db.session.get(User, int(user_id))
    if not user:
        return None, None
    # Primary check: role-based access
    if user.role not in [UserRole.VENDOR, UserRole.ADMIN]:
        # Fallback: if user has a storefront but role was never updated, heal and allow
        if user.storefront is not None:
            user.role = UserRole.VENDOR
            try:
                db.session.commit()
            except Exception:
                db.session.rollback()
        else:
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

    # Auto-generate referral code for users who registered before this feature
    if not user.referral_code:
        user.generate_referral_code()
        try:
            db.session.commit()
        except Exception:
            db.session.rollback()

    sf = user.storefront
    full_name = user.full_name
    
    from app.models.admin import VendorSubscription
    from datetime import datetime
    now = datetime.utcnow()
    active_sub = VendorSubscription.query.filter(
        VendorSubscription.vendor_id == int(user_id),
        VendorSubscription.status.in_(['ACTIVE', 'CANCELLED_PENDING_EXPIRY']),
        VendorSubscription.end_date > now
    ).order_by(VendorSubscription.end_date.desc()).first()
    
    plan_name = active_sub.plan.name if active_sub and active_sub.plan else "Free"
    plan_renews = active_sub.end_date.strftime("%b %d, %Y") if active_sub and active_sub.end_date else None

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
            "is_pro_verified": bool(sf.is_pro_active or sf.is_pro_verified),
            "is_pro_active": bool(sf.is_pro_active),
            "pro_verified_expires_at": sf.pro_verified_expires_at.isoformat() if sf.pro_verified_expires_at else None,
            "verification_status": sf.verification_status or "NOT_SUBMITTED",
            "account_type": sf.account_type or "INDIVIDUAL",
            "nin_document_url": sf.nin_document_url,
            "cac_document_url": sf.cac_document_url,
            "nin": user.nin,
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
            "plan_name": plan_name,
            "plan_renews": plan_renews,
            "has_active_subscription": bool(active_sub),
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
            "is_pro_verified": False,
            "is_pro_active": False,
            "pro_verified_expires_at": None,
            "verification_status": "NOT_SUBMITTED",
            "account_type": "INDIVIDUAL",
            "nin_document_url": None,
            "cac_document_url": None,
            "nin": user.nin,
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
            "plan_name": plan_name,
            "plan_renews": plan_renews,
            "has_active_subscription": bool(active_sub),
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
        "referral_code": user.referral_code or "",
        "telegram_id": user.telegram_id,
        "telegram_notification_prefs": user.telegram_notification_prefs or {},
        "personal_info": {
            "fullname": full_name,
            "email": user.email,
            "phone": user.phone or "",
            "referral_code": user.referral_code or "",
        },
        "store_settings": store_settings,
    }), 200



# ---------------------------------------------------------------------------
# GET /vendor/dashboard  — nudge stats for dashboard engagement cards
# ---------------------------------------------------------------------------

@vendor_bp.route('/dashboard', methods=['GET'])
@jwt_required()
def get_vendor_dashboard_stats():
    """Returns engagement nudge data + stats for the vendor dashboard home."""
    user_id = get_jwt_identity()
    user = db.session.get(User, int(user_id))
    if not user:
        return jsonify({"message": "User not found"}), 404

    sf = user.storefront
    store_view_count = sf.view_count if sf else 0

    # All orders for revenue + first-sale detection
    all_orders = Order.query.filter(
        Order.vendor_id == user.id,
        Order.status.in_(['PAID', 'SHIPPED', 'DELIVERED', 'COMPLETED', 'PENDING_DELIVERY'])
    ).all()
    total_orders = len(all_orders)
    total_revenue = sum(
        float(o.total_amount) for o in all_orders
        if o.status in ('COMPLETED', 'PAID', 'DELIVERED', 'SHIPPED')
    )
    is_first_sale = (total_orders == 1)

    # Products with views but no orders — nudge vendor to improve them
    nudge_products = []
    products = Product.query.filter_by(
        storefront_id=sf.id if sf else -1,
        is_active=True
    ).filter(
        db.or_(Product.is_deleted == False, Product.is_deleted.is_(None))
    ).all() if sf else []

    total_product_views = sum(p.view_count or 0 for p in products)

    from app.models.order import OrderItem
    for p in products:
        views = p.view_count or 0
        if views >= 3:
            order_item_count = db.session.query(OrderItem).filter_by(product_id=p.id).count()
            if order_item_count == 0:
                nudge_products.append({
                    "id": p.id,
                    "name": p.name,
                    "view_count": views,
                    "price": str(p.price),
                    "image": p.images[0] if p.images else None,
                })

    # Active sponsored listings
    from app.models.admin import SponsoredListing
    from datetime import datetime as _dt, timezone as _tz
    _now = _dt.now(_tz.utc)
    active_sponsored = SponsoredListing.query.filter(
        SponsoredListing.vendor_id == user.id,
        SponsoredListing.is_active == True,
        SponsoredListing.end_date > _now
    ).all() if sf else []
    sponsored_product_ids = [s.product_id for s in active_sponsored]

    return jsonify({
        "store_view_count": store_view_count,
        "total_product_views": total_product_views,
        "nudge_products": nudge_products,
        "is_first_sale": is_first_sale,
        "total_orders": total_orders,
        "total_revenue": total_revenue,
        "sponsored_product_ids": sponsored_product_ids,
        "pro_verified": {
            "is_pro_verified": bool(sf.is_pro_verified) if sf else False,
            "expires_at": sf.pro_verified_expires_at.isoformat() if (sf and sf.pro_verified_expires_at) else None,
            "verification_status": sf.verification_status if sf else "NOT_SUBMITTED",
        },
    }), 200


# ---------------------------------------------------------------------------
# POST /vendor/onboard & GET /vendor/check-slug
# ---------------------------------------------------------------------------

def _build_default_template_options(category_str=""):
    cat = (category_str or "").lower().strip()
    
    if any(k in cat for k in ['fashion', 'cloth', 'apparel', 'wear', 'shoe', 'bag', 'dress', 'boutique', 'luxury']):
        return {
            "layout_style": "Fashion Chic",
            "theme": "Fashion Chic",
            "palette_id": "Warm Luxe",
            "primary_color": "#111111",
            "primaryColor": "#111111",
            "secondary_color": "#C9A86A",
            "secondaryColor": "#C9A86A",
            "font_heading": "Playfair Display, serif",
            "font_body": "Inter, sans-serif",
            "hero_layout": "centered",
            "hero_heading": "Curated Modern Fashion & Style",
            "hero_subtext": "Discover premium apparel, footwear and accessories crafted for timeless elegance.",
            "hero_cta": "Shop Collection",
            "about_title": "About Our Brand",
            "about_description": "We deliver authentic, stylish, and high-quality fashion pieces designed to elevate your everyday wardrobe.",
            "store_tagline": "Elegance & Style in Every Stitch",
            "sections": ["hero", "products", "about", "contact"],
        }
    elif any(k in cat for k in ['tech', 'gadget', 'electronic', 'phone', 'computer', 'laptop', 'device', 'solar', 'appliance']):
        return {
            "layout_style": "Modern Tech",
            "theme": "Modern Tech",
            "palette_id": "Ocean Blue",
            "primary_color": "#0A0F1D",
            "primaryColor": "#0A0F1D",
            "secondary_color": "#0066FF",
            "secondaryColor": "#0066FF",
            "font_heading": "Plus Jakarta Sans, sans-serif",
            "font_body": "Inter, sans-serif",
            "hero_layout": "split",
            "hero_heading": "Next-Gen Tech & Authentic Gadgets",
            "hero_subtext": "Top-tier electronics, smart devices, and accessories with verified warranty and fast delivery.",
            "hero_cta": "Explore Gadgets",
            "about_title": "Why Buy From Us",
            "about_description": "We specialize in 100% genuine electronics, guaranteed warranty, and seamless customer support.",
            "store_tagline": "Empowering Your Digital Lifestyle",
            "sections": ["hero", "products", "about", "contact"],
        }
    elif any(k in cat for k in ['beauty', 'cosmetic', 'skincare', 'hair', 'wellness', 'fragrance', 'perfume', 'makeup']):
        return {
            "layout_style": "Beauty & Wellness",
            "theme": "Beauty & Wellness",
            "palette_id": "Rose Gold",
            "primary_color": "#2D1A22",
            "primaryColor": "#2D1A22",
            "secondary_color": "#E87A90",
            "secondaryColor": "#E87A90",
            "font_heading": "Playfair Display, serif",
            "font_body": "Inter, sans-serif",
            "hero_layout": "centered",
            "hero_heading": "Pure Radiance & Organic Beauty",
            "hero_subtext": "Nourish your skin and hair with clean, organic, and dermatologist-tested beauty essentials.",
            "hero_cta": "Find Your Glow",
            "about_title": "Our Beauty Promise",
            "about_description": "Specially formulated products for all skin and hair textures with authentic, skin-safe ingredients.",
            "store_tagline": "Glow With Natural Confidence",
            "sections": ["hero", "products", "about", "contact"],
        }
    elif any(k in cat for k in ['food', 'restaurant', 'meal', 'grocery', 'drink', 'beverage', 'cake', 'bakery', 'snack', 'cater']):
        return {
            "layout_style": "Street Food",
            "theme": "Street Food",
            "palette_id": "Warm Bronze",
            "primary_color": "#0B1B3B",
            "primaryColor": "#0B1B3B",
            "secondary_color": "#E0921C",
            "secondaryColor": "#E0921C",
            "font_heading": "Plus Jakarta Sans, sans-serif",
            "font_body": "Inter, sans-serif",
            "hero_layout": "banner",
            "hero_heading": "Fresh & Delicious Meals Delivered Fast",
            "hero_subtext": "Savour authentic flavours, freshly prepared dishes, and pantry essentials right to your doorstep.",
            "hero_cta": "Order Fresh Now",
            "about_title": "Freshness & Taste Guaranteed",
            "about_description": "We use the highest quality fresh ingredients to bring you hygienic and unforgettable tastes every day.",
            "store_tagline": "Taste the Fresh Difference",
            "sections": ["hero", "products", "about", "contact"],
        }
    else:
        return {
            "layout_style": "African Market",
            "theme": "African Market",
            "palette_id": "Classic Navy",
            "primary_color": "#0B1B3B",
            "primaryColor": "#0B1B3B",
            "secondary_color": "#E0921C",
            "secondaryColor": "#E0921C",
            "font_heading": "Plus Jakarta Sans, sans-serif",
            "font_body": "Inter, sans-serif",
            "hero_layout": "centered",
            "hero_heading": "Quality Products & Trusted Service",
            "hero_subtext": "Explore our full catalog of authentic products backed by Siiqo escrow buyer protection.",
            "hero_cta": "Browse Catalog",
            "about_title": "About Our Store",
            "about_description": "Your trusted vendor for authentic goods, prompt dispatch, and dedicated customer care.",
            "store_tagline": "Quality You Can Trust",
            "sections": ["hero", "products", "about", "contact"],
        }


@vendor_bp.route('/check-slug', methods=['GET'])
@jwt_required()
def check_slug():
    slug = request.args.get('slug', '').lower().strip()
    if not slug:
        return jsonify({"available": False, "message": "Slug is required"}), 400
        
    RESERVED_WORDS = ['api', 'admin', 'www', 'auth', 'cart', 'checkout', 'dashboard', 'marketplace', 'vendor', 'buyer', 'community', 'finance-tools']
    if slug in RESERVED_WORDS:
        return jsonify({"available": False, "message": "Reserved keyword"}), 200
        
    exists = Storefront.query.filter_by(store_slug=slug).first() is not None
    return jsonify({"available": not exists}), 200

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

    # Auto-generate unique slug or use provided slug
    base_slug = data.get('store_slug') or re.sub(r'[^a-z0-9]+', '-', store_name.lower()).strip('-')
    store_slug = base_slug
    
    # RESERVED WORDS CHECK
    RESERVED_WORDS = ['api', 'admin', 'www', 'auth', 'cart', 'checkout', 'dashboard', 'marketplace', 'vendor', 'buyer', 'community', 'finance-tools']
    if store_slug in RESERVED_WORDS:
        return jsonify({"message": f"The store URL '{store_slug}' is a reserved system keyword. Please choose another."}), 400
        
    if Storefront.query.filter_by(store_slug=store_slug).first():
        return jsonify({"message": f"The store URL '{store_slug}' is already taken. Please choose another."}), 400

    user.role = UserRole.VENDOR

    # Determine category & default template options
    category_val = data.get('category') or data.get('business_category') or ''
    if not category_val and data.get('description'):
        category_val = data.get('description')
    
    default_template = _build_default_template_options(category_val)
    if 'template_options' in data:
        incoming_tpl = data['template_options']
        if isinstance(incoming_tpl, str):
            try:
                incoming_tpl = json.loads(incoming_tpl)
            except Exception:
                incoming_tpl = {}
        if isinstance(incoming_tpl, dict) and incoming_tpl:
            default_template.update(incoming_tpl)

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
        account_type=data.get('account_type', 'INDIVIDUAL'),
        theme_color=default_template.get('primary_color', '#0b1b3b'),
        template_options=default_template,
        # Auto-publish so vendor's store is immediately live upon onboarding.
        # Identity review / Verified badge is handled separately via KYC documents (NIN / CAC) or Pro Verified subscription.
        is_published=True,
        is_verified=False,
    )

    if 'nin' in data:
        user.nin = data['nin']

    logo_file = request.files.get('logo') or request.files.get('store_logo')
    banner_file = request.files.get('banner') or request.files.get('banner_url')
    nin_doc = request.files.get('nin_document') or request.files.get('nin_doc')
    cac_doc = request.files.get('cac_document') or request.files.get('cac_doc')

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

    if nin_doc:
        try:
            storefront.nin_document_url = save_uploaded_file(nin_doc, subfolder='verifications')
        except ValueError as e:
            return jsonify({"message": str(e)}), 400

    if cac_doc:
        try:
            storefront.cac_document_url = save_uploaded_file(cac_doc, subfolder='verifications')
        except ValueError as e:
            return jsonify({"message": str(e)}), 400

    # Auto-flag as pending verification if CAC/NIN or verification files are submitted
    if storefront.cac_reg or user.nin or storefront.nin_document_url or storefront.cac_document_url:
        storefront.verification_status = 'PENDING_VERIFY_SUB'

    db.session.add(storefront)
    db.session.commit()

    # ── Paystack subaccount (for Split Payments on digital/service checkouts) ──
    # If bank details were supplied at onboarding, register the vendor as a
    # Paystack subaccount immediately so future checkouts can split the payment.
    if storefront.bank_code and storefront.account_number:
        try:
            from app.services.escrow.paystack_provider import create_paystack_subaccount
            sub_result = create_paystack_subaccount(
                business_name=storefront.store_name,
                bank_code=storefront.bank_code,
                account_number=storefront.account_number,
            )
            if sub_result.get("success"):
                storefront.paystack_subaccount_code = sub_result["subaccount_code"]
                db.session.commit()
        except Exception as _sub_exc:
            logging.warning(f"[ONBOARDING] Paystack subaccount creation failed for vendor {user.id}: {_sub_exc}")

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
        "message": "Vendor onboarding complete! Your storefront is now live.",
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
        'account_type': 'account_type',
        'meta_title': 'meta_title',
        'meta_description': 'meta_description',
        'logo_url': 'store_logo',
    }
    for form_key, model_attr in field_map.items():
        if form_key in data:
            setattr(sf, model_attr, data[form_key])
            
    # Also update user personal info if provided
    if 'phone' in data:
        user.phone = data['phone']

    if 'nin' in data:
        user.nin = data['nin']
        
    if 'fullname' in data:
        name_parts = data['fullname'].strip().split(' ', 1)
        user.first_name = name_parts[0]
        if len(name_parts) > 1:
            user.last_name = name_parts[1]
        else:
            user.last_name = ""

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

    # Verification file uploads
    uploaded_doc = False
    if 'nin_document' in request.files or 'nin_doc' in request.files:
        nin_file = request.files.get('nin_document') or request.files.get('nin_doc')
        try:
            sf.nin_document_url = save_uploaded_file(nin_file, subfolder='verifications')
            uploaded_doc = True
        except ValueError as e:
            return jsonify({"message": str(e)}), 400

    if 'cac_document' in request.files or 'cac_doc' in request.files:
        cac_file = request.files.get('cac_document') or request.files.get('cac_doc')
        try:
            sf.cac_document_url = save_uploaded_file(cac_file, subfolder='verifications')
            uploaded_doc = True
        except ValueError as e:
            return jsonify({"message": str(e)}), 400

    # Auto-flag as pending if identity details or documents are updated and not verified yet
    if 'cac_reg' in data or 'nin' in data or uploaded_doc:
        if sf.verification_status != 'VERIFIED':
            sf.verification_status = 'PENDING_VERIFY_SUB'

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

@vendor_bp.route('/products/scrape', methods=['POST'])
@jwt_required()
def scrape_product():
    user_id = get_jwt_identity()
    user, sf = _require_vendor_storefront(user_id)
    if not user or not sf:
        return jsonify({"message": "Vendor access required"}), 403
    
    data = request.get_json() or {}
    url = data.get('url')
    if not url:
        return jsonify({"message": "URL is required"}), 400
        
    result = scrape_product_url(url)
    if not result.get("success"):
        return jsonify({"message": result.get("message", "Failed to scrape URL")}), 400
        
    return jsonify(result), 200

@vendor_bp.route('/products/add', methods=['POST'])
@jwt_required()
def add_product():
    user_id = get_jwt_identity()
    user, sf = _require_vendor_storefront(user_id)
    if not user:
        return jsonify({"message": "Vendor access required"}), 403
    if not sf:
        return jsonify({"message": "Complete vendor onboarding first"}), 403

    # ── FREE PLAN: 10-product lifetime limit ─────────────────────────────────
    # Count ALL products ever created (including deleted/inactive) so vendors
    # can't bypass the limit by deleting and re-listing.
    from app.models.admin import VendorSubscription
    from datetime import datetime as _dt_limit
    _now_limit = _dt_limit.utcnow()
    _active_sub = VendorSubscription.query.filter(
        VendorSubscription.vendor_id == user.id,
        VendorSubscription.status.in_(['ACTIVE', 'CANCELLED_PENDING_EXPIRY']),
        VendorSubscription.end_date > _now_limit,
    ).first()
    _is_pro = _active_sub is not None

    if not _is_pro:
        _total_ever = Product.query.filter_by(storefront_id=sf.id).count()
        if _total_ever >= 10:
            return jsonify({
                "message": "You've reached the 10-product limit on the free plan. Upgrade to Pro for unlimited listings and other premium features.",
                "upgrade_required": True,
                "code": "PRODUCT_LIMIT_REACHED",
            }), 403
    # ─────────────────────────────────────────────────────────────────────────

    data = request.form if request.form else (request.get_json() or {})

    # Accept both 'name' (AddProductModal) and 'product_name' (page.tsx handleSaveProduct)
    name = (data.get('name') or data.get('product_name') or '').strip()
    if not name:
        return jsonify({"message": "Product name is required"}), 400

    # Accept 'price', 'price-text', or 'product_price' (frontend sends product_price in cents — divide by 100)
    raw_price_str = data.get('price') or data.get('price-text') or data.get('product_price') or '0'
    try:
        raw_price = float(raw_price_str)
        # If the value looks like it was multiplied by 100 (>= 100 and no decimal context), divide back
        # We detect this by checking if 'product_price' was the key used (page.tsx path)
        if data.get('product_price') and not data.get('price'):
            price = raw_price / 100.0
        else:
            price = raw_price
    except (ValueError, TypeError):
        return jsonify({"message": "Invalid price"}), 400

    # Accept 'stock_quantity', 'quantity' (page.tsx sends 'quantity')
    stock_raw = data.get('stock_quantity') or data.get('quantity') or 1
    try:
        stock_qty = int(stock_raw)
    except (ValueError, TypeError):
        stock_qty = 1

    # Check for duplicate active product names under the same vendor
    existing_product = Product.query.filter(
        Product.storefront_id == sf.id,
        Product.name.ilike(name),
        Product.is_active == True
    ).first()
    if existing_product:
        return jsonify({
            "message": f"You already have an active product named '{name}'. Please use a different name or edit the existing listing.",
            "code": "DUPLICATE_PRODUCT_NAME"
        }), 409

    # Resolve category: accept category_id (int) or category (name string → look up id)
    category_id = None
    if data.get('category_id'):
        try:
            category_id = int(data['category_id'])
        except (ValueError, TypeError):
            pass
    elif data.get('category'):
        cat = Category.query.filter(
            Category.name.ilike(data['category'].strip()) |
            Category.slug.ilike(data['category'].strip())
        ).first()
        if cat:
            category_id = cat.id

    # Validate type-specific required fields & resolve product type
    p_type = data.get('product_type', 'physical')

    # Handle digital file upload if provided
    file_url_val = data.get('file_url')
    booking_link_val = data.get('booking_link')
    digital_file = request.files.get('digital_file') or request.files.get('file')
    if digital_file and digital_file.filename:
        try:
            uploaded_url = save_uploaded_file(digital_file, subfolder='digital_products', is_digital=True)
            if uploaded_url:
                if p_type == 'service':
                    booking_link_val = uploaded_url
                else:
                    file_url_val = uploaded_url
        except ValueError as e:
            return jsonify({"message": str(e)}), 400
    if p_type == 'digital' and not (file_url_val or '').strip():
        return jsonify({"message": "A Digital File upload or Download Link (file_url) is required for digital products."}), 400
    if p_type == 'service' and not (booking_link_val or '').strip():
        return jsonify({"message": "A Booking Link or Intake Document (booking_link) is required for service products."}), 400

    new_product = Product(
        storefront_id=sf.id,
        name=name,
        description=data.get('description', ''),
        price=price,
        stock_quantity=stock_qty,
        category_id=category_id,
        condition=data.get('condition') or None,
        location=data.get('location'),
        latitude=float(data['latitude']) if data.get('latitude') else None,
        longitude=float(data['longitude']) if data.get('longitude') else None,
        is_negotiable=str(data.get('is_negotiable', 'false')).lower() in ('true', '1', 'yes'),
        floor_price=float(data['floor_price']) if data.get('floor_price') else None,
        product_type=data.get('product_type', 'physical'),
        is_free=(price == 0 or str(data.get('is_free', 'false')).lower() in ('true', '1', 'yes')),
        file_url=file_url_val,
        booking_link=booking_link_val,
        sku=data.get('sku'),
        weight=float(data['weight']) if data.get('weight') else None,
        seo_title=data.get('seo_title') or data.get('seoTitle'),
        seo_description=data.get('seo_description') or data.get('seoDescription'),
    )

    # ── Category-specific attributes (new, additive) ──────────────────────
    if data.get('attributes'):
        import json as _json
        attrs = data['attributes']
        if isinstance(attrs, str):
            try:
                attrs = _json.loads(attrs)
            except Exception:
                attrs = None
        new_product.attributes = attrs

    # Handle multiple image files (frontend appends each as 'images')
    # Also accept legacy single-file keys 'image' and 'images-file'
    saved_images = []
    image_files = request.files.getlist('images')
    if not image_files:
        single = request.files.get('image') or request.files.get('images-file')
        if single:
            image_files = [single]
            
    # Enforce MAX 5 images per product
    if len(image_files) > 5:
        return jsonify({
            "message": "Too many images. Maximum 5 images allowed per product.",
            "code": "IMAGE_LIMIT_EXCEEDED"
        }), 400
    for img_file in image_files:
        if img_file and img_file.filename:
            try:
                saved_url = save_uploaded_file(img_file, subfolder='products')
                if saved_url:
                    saved_images.append(saved_url)
            except ValueError as e:
                return jsonify({"message": str(e)}), 400
    if saved_images:
        new_product.images = saved_images

    db.session.add(new_product)
    db.session.flush()  # get new_product.id before commit

    # Auto-post to community feed as PRODUCT_LAUNCH
    # Only if vendor opted in (announce_to_community param) or always for new products
    announce = str(data.get('announce_to_community', 'true')).lower() not in ('false', '0', 'no')
    if announce:
        try:
            from app.models.social import Post as CommunityPost
            from datetime import datetime, timedelta, timezone

            # Deduplication: check if a PRODUCT_LAUNCH post was created by this user in the last 30s
            recent_threshold = datetime.now(timezone.utc) - timedelta(seconds=30)
            existing_post = CommunityPost.query.filter(
                CommunityPost.user_id == user_id,
                CommunityPost.post_type == 'PRODUCT_LAUNCH',
                CommunityPost.created_at >= recent_threshold
            ).first()

            if not existing_post:
                from app.routes.community import _clean_community_text
                store_name = sf.store_name or user.full_name or 'A vendor'
                clean_desc = _clean_community_text(new_product.description or '')
                desc_snippet = (clean_desc[:140] + '...') if len(clean_desc) > 140 else clean_desc
                product_url = f"https://siiqo.com/marketplace?product={new_product.id}"
                store_url = f"https://siiqo.com/{sf.store_slug}"

                post_content = (
                    f"🚀 NEW PRODUCT LAUNCH!\n\n"
                    f"✨ {name} — ₦{price:,.0f}\n"
                )
                if desc_snippet:
                    post_content += f"\n📝 {desc_snippet}\n"
                post_content += (
                    f"\n🏪 Store: {store_name}\n"
                    f"👉 View & Purchase: {product_url}\n"
                    f"🏬 Visit Storefront: {store_url}"
                )

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
    
    # Sync to Algolia
    try:
        sync_product_to_algolia(new_product)
    except Exception as e:
        logging.warning(f"Failed to sync to Algolia: {e}")

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

    # Accept both 'name' and 'product_name'
    if 'name' in data:
        product.name = data['name']
    elif 'product_name' in data:
        product.name = data['product_name']

    if 'description' in data:
        product.description = data['description']

    if 'condition' in data:
        product.condition = data['condition']
    if 'location' in data:
        product.location = data['location']
    if 'latitude' in data:
        try:
            product.latitude = float(data['latitude'])
        except (ValueError, TypeError):
            product.latitude = None
    if 'longitude' in data:
        try:
            product.longitude = float(data['longitude'])
        except (ValueError, TypeError):
            product.longitude = None

    # Accept 'price', 'price-text', or 'product_price' (product_price may be in cents)
    if 'product_price' in data and 'price' not in data:
        try:
            product.price = float(data['product_price']) / 100.0
        except (ValueError, TypeError):
            pass
    elif 'price' in data or 'price-text' in data:
        try:
            product.price = float(data.get('price') or data.get('price-text'))
        except (ValueError, TypeError):
            pass

    if product.price == 0 or str(data.get('is_free')).lower() in ('true', '1', 'yes'):
        product.is_free = True
    elif 'is_free' in data:
        product.is_free = str(data.get('is_free')).lower() in ('true', '1', 'yes')

    # Accept 'stock_quantity' or 'quantity'
    if 'stock_quantity' in data:
        try:
            product.stock_quantity = int(data['stock_quantity'])
        except (ValueError, TypeError):
            pass
    elif 'quantity' in data:
        try:
            product.stock_quantity = int(data['quantity'])
        except (ValueError, TypeError):
            pass

    # Accept 'is_active' or 'status'
    if 'is_active' in data:
        product.is_active = str(data['is_active']).lower() in ('true', '1', 'yes')
    elif 'status' in data:
        product.is_active = str(data['status']).lower() in ('active', 'true', '1')

    # Resolve category: accept category_id (int) or category (name string)
    if 'category_id' in data:
        try:
            product.category_id = int(data['category_id']) if data['category_id'] else None
        except (ValueError, TypeError):
            product.category_id = None
    elif 'category' in data and data['category']:
        cat = Category.query.filter(
            Category.name.ilike(data['category'].strip()) |
            Category.slug.ilike(data['category'].strip())
        ).first()
    if 'file_url' in data:
        product.file_url = data['file_url']
    if 'booking_link' in data:
        product.booking_link = data['booking_link']

    digital_file = request.files.get('digital_file') or request.files.get('file')
    if digital_file and digital_file.filename:
        try:
            uploaded_url = save_uploaded_file(digital_file, subfolder='digital_products', is_digital=True)
            if uploaded_url:
                if product.product_type == 'service':
                    product.booking_link = uploaded_url
                else:
                    product.file_url = uploaded_url
        except ValueError as e:
            return jsonify({"message": str(e)}), 400

    # Handle multiple image files
    saved_images = []
    image_files = request.files.getlist('images')
    if not image_files:
        single = request.files.get('image') or request.files.get('images-file')
        if single:
            image_files = [single]
            
    current_images_count = len(product.images or [])
    if current_images_count + len(image_files) > 5:
        return jsonify({
            "message": f"Too many images. You have {current_images_count} existing images and are uploading {len(image_files)} more. Maximum 5 allowed.",
            "code": "IMAGE_LIMIT_EXCEEDED"
        }), 400
    for img_file in image_files:
        if img_file and img_file.filename:
            try:
                saved_url = save_uploaded_file(img_file, subfolder='products')
                if saved_url:
                    saved_images.append(saved_url)
            except ValueError as e:
                return jsonify({"message": str(e)}), 400
    if saved_images:
        product.images = list(product.images or []) + saved_images

    # Negotiation fields
    if 'is_negotiable' in data:
        product.is_negotiable = str(data['is_negotiable']).lower() in ('true', '1', 'yes')
    if 'floor_price' in data:
        try:
            product.floor_price = float(data['floor_price']) if data['floor_price'] else None
        except (ValueError, TypeError):
            pass

    # Extended Siiqo fields
    if 'product_type' in data:
        product.product_type = data['product_type']
    if 'file_url' in data:
        product.file_url = data['file_url'] or None
    if 'booking_link' in data:
        product.booking_link = data['booking_link'] or None

    # Validate type-specific required fields after applying updates
    effective_type = product.product_type or 'physical'
    if effective_type == 'digital' and not product.file_url:
        return jsonify({"message": "A Download Link (file_url) is required for digital products."}), 400
    if effective_type == 'service' and not product.booking_link:
        return jsonify({"message": "A Booking Link (booking_link) is required for service products."}), 400

    if 'sku' in data:
        product.sku = data['sku']
    if 'weight' in data:
        try:
            product.weight = float(data['weight']) if data['weight'] else None
        except (ValueError, TypeError):
            pass
    if 'seo_title' in data:
        product.seo_title = data['seo_title']
    elif 'seoTitle' in data:
        product.seo_title = data['seoTitle']
    if 'seo_description' in data:
        product.seo_description = data['seo_description']
    elif 'seoDescription' in data:
        product.seo_description = data['seoDescription']

    # ── Category-specific attributes (new, additive, non-breaking) ──────────
    if 'attributes' in data:
        import json as _json
        attrs = data['attributes']
        if isinstance(attrs, str):
            try:
                attrs = _json.loads(attrs)
            except Exception:
                attrs = None
        product.attributes = attrs

    db.session.commit()
    
    # Sync to Algolia
    try:
        sync_product_to_algolia(product)
    except Exception as e:
        logging.warning(f"Failed to sync to Algolia: {e}")

    return jsonify({"message": "Product updated successfully", "status": "success"}), 200

@vendor_bp.route('/products/delete/<int:product_id>', methods=['DELETE'])
@jwt_required()
def delete_product(product_id):
    user_id = get_jwt_identity()
    user, sf = _require_vendor_storefront(user_id)
    if not user:
        return jsonify({"message": "Vendor access required"}), 403

    product = db.session.get(Product, product_id)
    if not product or (sf and product.storefront_id != sf.id):
        return jsonify({"message": "Product not found"}), 404

    # Perform SOFT delete
    product.is_deleted = True
    product.is_active = False
    db.session.commit()
    
    # Remove from Algolia
    try:
        delete_product_from_algolia(product_id)
    except Exception as e:
        logging.warning(f"Failed to delete from Algolia: {e}")
        
    return jsonify({"message": "Product deleted", "status": "success"}), 200

@vendor_bp.route('/products/my-products', methods=['GET'])
@jwt_required()
def my_products():
    user_id = get_jwt_identity()
    user, sf = _require_vendor_storefront(user_id)
    if not user or not sf:
        return jsonify([]), 200

    page = request.args.get('page', type=int)
    limit = request.args.get('limit', type=int)

    # Exclude soft-deleted products from the dashboard (handling both False and NULL)
    query = Product.query.filter(
        Product.storefront_id == sf.id,
        db.or_(Product.is_deleted == False, Product.is_deleted.is_(None))
    ).options(joinedload(Product.category))
    
    if page and limit:
        paginated = query.paginate(page=page, per_page=limit, error_out=False)
        products = paginated.items
    else:
        products = query.limit(500).all()
    products_data = [{
        "id": p.id,
        "name": p.name,
        "price": str(p.price),
        "stock": p.stock_quantity,
        "quantity": p.stock_quantity,          # alias so frontend quantity mapping works
        "is_active": p.is_active,
        "status": "active" if p.is_active else "inactive",
        "images": p.images or [],
        "description": p.description,
        "category_id": p.category_id,
        "category": p.category.name if p.category else "",   # resolved name
        "vendor_id": sf.vendor_id,             # needed for cart vendor filtering
        "is_negotiable": p.is_negotiable,
        "condition": p.condition,
        "location": p.location,
        "latitude": p.latitude,
        "longitude": p.longitude,
        "floor_price": str(p.floor_price) if p.floor_price else None,
        "product_type": p.product_type,
        "file_url": p.file_url,
        "booking_link": p.booking_link,
        "sku": p.sku,
        "weight": str(p.weight) if p.weight else None,
        "seo_title": p.seo_title,
        "seoTitle": p.seo_title,
        "seo_description": p.seo_description,
        "seoDescription": p.seo_description,
        "created_at": p.created_at.isoformat() if p.created_at else None,
        "createdAt": p.created_at.isoformat() if p.created_at else None,  # camelCase alias
    } for p in products]

    if page and limit:
        return jsonify({"data": products_data, "total": paginated.total, "pages": paginated.pages}), 200
    return jsonify(products_data), 200


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

    page = request.args.get('page', type=int)
    limit = request.args.get('limit', type=int)

    # N+1 Optimization
    from app.models.order import OrderItem
    query = Order.query.filter_by(vendor_id=user.id).order_by(Order.created_at.desc()).options(
        joinedload(Order.buyer),
        joinedload(Order.items).joinedload(OrderItem.product)
    )

    if page and limit:
        paginated = query.paginate(page=page, per_page=limit, error_out=False)
        orders = paginated.items
    else:
        orders = query.limit(500).all()

    # ── Live Paystack sync for stuck PENDING orders ───────────────────────────
    from app.models.escrow import EscrowTransaction
    from datetime import datetime, timezone as _tz
    needs_commit = False
    for o in orders:
        if o.status == 'PENDING':
            escrow_check = EscrowTransaction.query.filter_by(order_id=o.id).first()
            if escrow_check and escrow_check.transaction_number:
                try:
                    from app.services.escrow.paystack_provider import PaystackProvider
                    verify = PaystackProvider().verify_transaction(escrow_check.transaction_number)
                    if verify.get("success"):
                        escrow_check.status = 'IN_ESCROW'
                        escrow_check.paid_at = escrow_check.paid_at or datetime.now(_tz.utc)
                        o.status = 'PAID'
                        needs_commit = True
                except Exception:
                    pass  # non-fatal
    if needs_commit:
        try:
            db.session.commit()
        except Exception:
            db.session.rollback()
    # ─────────────────────────────────────────────────────────────────────────

    orders_data = [{
        "id": o.id,
        "total_amount": str(o.total_amount),
        "status": o.status,
        "created_at": o.created_at.isoformat() if o.created_at else None,
        "payment_method": o.payment_method or "ESCROW",
        "buyer_id": o.buyer_id,
        "buyer": {
            "id": o.buyer_id,
            "name": o.buyer.full_name if o.buyer else "Unknown",
            "email": o.buyer.email if o.buyer else "",
            "phone": o.buyer.phone if o.buyer else "",
        },
        "shipping_address": {
            "name": o.delivery_name or (o.buyer.full_name if o.buyer else "Unknown"),
            "street": o.delivery_address or "N/A",
            "city": o.delivery_city or "",
            "state": o.delivery_state or "",
            "phone": o.delivery_phone or (o.buyer.phone if o.buyer else ""),
            "country": "Nigeria",
            "zipCode": ""
        },
        "items": [{
            "product_id": item.product_id,
            "name": item.product.name if item.product else "Unknown",
            "quantity": item.quantity,
            "price": str(item.price_at_purchase),
        } for item in o.items],
    } for o in orders]

    if page and limit:
        return jsonify({"data": orders_data, "total": paginated.total, "pages": paginated.pages}), 200
    return jsonify(orders_data), 200


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

    body = request.get_json() or {}
    new_status = (body.get('status') or '').upper().strip()
    if not new_status:
        return jsonify({"message": "Status is required"}), 400

    ALLOWED_STATUSES = [
        'PENDING', 'PAID', 'PENDING_DELIVERY', 'PROCESSING',
        'SHIPPED', 'DELIVERED', 'COMPLETED', 'CANCELLED',
    ]
    if new_status not in ALLOWED_STATUSES:
        return jsonify({"message": f"Invalid status. Allowed: {', '.join(ALLOWED_STATUSES)}"}), 400

    if order.payment_method == 'ESCROW' and new_status == 'COMPLETED':
        return jsonify({"message": "You cannot manually complete an Escrow order. The buyer must confirm delivery to release funds."}), 400

    # Stock Restoration
    if new_status == 'CANCELLED' and order.status != 'CANCELLED':
        for item in order.items:
            if item.product:
                item.product.stock_quantity = (item.product.stock_quantity or 0) + item.quantity
        
        # Also sync EscrowTransaction status if applicable
        from app.models.escrow import EscrowTransaction, EscrowStatus
        if order.payment_method == 'ESCROW':
            escrow = EscrowTransaction.query.filter_by(order_id=order.id).first()
            if escrow and escrow.status == EscrowStatus.PENDING_PAYMENT:
                escrow.status = EscrowStatus.CANCELLED

    order.status = new_status

    # Save tracking number if provided alongside the status update
    tracking_number = (body.get('tracking_number') or body.get('trackingNumber') or '').strip()
    if tracking_number:
        order.tracking_number = tracking_number

    # Keep EscrowTransaction.status in sync with the order shipping status.
    # This is required so that auto-release timer and delivery reminders work correctly —
    # both tasks query EscrowTransaction.status == DELIVERED, not Order.status.
    from app.models.escrow import EscrowTransaction, EscrowStatus
    if order.payment_method == 'ESCROW' and new_status in ('SHIPPED', 'DELIVERED'):
        escrow = EscrowTransaction.query.filter_by(order_id=order.id).first()
        if escrow and escrow.status == EscrowStatus.IN_ESCROW and new_status == 'SHIPPED':
            escrow.status = EscrowStatus.SHIPPED
        elif escrow and escrow.status in (EscrowStatus.IN_ESCROW, EscrowStatus.SHIPPED) and new_status == 'DELIVERED':
            escrow.status = EscrowStatus.DELIVERED

    # Notify buyer of the status change
    from app.models.communication import Notification
    status_labels = {
        'PROCESSING': 'is being processed',
        'SHIPPED': 'has been shipped',
        'DELIVERED': 'has been marked as delivered',
        'COMPLETED': 'is complete',
        'CANCELLED': 'has been cancelled',
        'PENDING_DELIVERY': 'is pending delivery',
    }
    label = status_labels.get(new_status, f'status updated to {new_status}')
    db.session.add(Notification(
        user_id=order.buyer_id,
        title=f"Order #{order_id} Update",
        message=f"Your order #{order_id} from {user.storefront.store_name if user.storefront else 'vendor'} {label}.",
        type="ORDER",
        order_id=order_id,
    ))

    try:
        db.session.commit()
    except Exception as e:
        db.session.rollback()
        import logging, traceback
        logging.error(f"Failed to update order status: {str(e)}\n{traceback.format_exc()}")
        return jsonify({"message": "Failed to update order status", "error": "Internal database error."}), 500

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

    records = Ledger.query.filter_by(vendor_id=user.id).order_by(Ledger.created_at.desc()).limit(500).all()

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

    # ── Subscription Gate: Pro required for CRM Customer Data ──────────────
    from app.models.admin import VendorSubscription
    from datetime import datetime as _dt_crm
    _now_crm = _dt_crm.utcnow()
    active_sub = VendorSubscription.query.filter(
        VendorSubscription.vendor_id == user.id,
        VendorSubscription.status.in_(['ACTIVE', 'CANCELLED_PENDING_EXPIRY']),
        VendorSubscription.end_date > _now_crm
    ).first()

    if not active_sub:
        return jsonify({
            "message": "Pro subscription required to access CRM customer records and marketing tools.",
            "upgrade_required": True,
            "code": "SUBSCRIPTION_REQUIRED"
        }), 403

    page = request.args.get('page', type=int)
    limit = request.args.get('limit', type=int)

    query = CustomerProfile.query.filter_by(vendor_id=user.id).options(joinedload(CustomerProfile.buyer))
    
    if page and limit:
        paginated = query.paginate(page=page, per_page=limit, error_out=False)
        profiles = paginated.items
    else:
        profiles = query.limit(500).all()

    customers_data = [{
        "buyer_id": p.buyer_id,
        "name": p.buyer.full_name if p.buyer else "Unknown",
        "email": p.buyer.email if p.buyer else "",
        "total_spent": str(p.total_spent),
        "total_orders": p.total_orders,
        "segment": p.segment,
        "last_purchase_date": p.last_purchase_date.isoformat() if p.last_purchase_date else None,
        "tags": p.tags if hasattr(p, 'tags') else [],
    } for p in profiles]

    if page and limit:
        return jsonify({
            "status": "success",
            "customers": customers_data,
            "total": paginated.total,
            "pages": paginated.pages
        }), 200

    return jsonify({
        "status": "success",
        "customers": customers_data,
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
        coupons = Coupon.query.filter_by(vendor_id=user.id).limit(500).all()
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
        campaigns = Campaign.query.filter_by(vendor_id=user.id).limit(500).all()
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

# ---------------------------------------------------------------------------
# POST /vendor/storefront/analyze
# ---------------------------------------------------------------------------

@vendor_bp.route('/storefront/analyze', methods=['POST'])
@jwt_required()
def analyze_storefront():
    user_id = get_jwt_identity()
    user = db.session.get(User, int(user_id))
    if not user:
        return jsonify({"message": "User not found"}), 404

    data = request.get_json() or {}
    url = data.get('url')
    if not url:
        return jsonify({"message": "URL is required"}), 400

    result = analyze_storefront_url(url)
    if not result.get("success"):
        return jsonify({"message": result.get("message", "Analysis failed")}), 400

    return jsonify(result), 200


# ---------------------------------------------------------------------------
# GET /vendor/trust-profile
# ---------------------------------------------------------------------------

@vendor_bp.route('/trust-profile', methods=['GET'])
@jwt_required()
def get_vendor_trust_profile():
    user_id = get_jwt_identity()
    from app.services.trust import get_or_create_trust_profile, recalculate_vendor_trust
    
    # Recalculate on load to ensure dashboard displays up-to-date data
    profile = recalculate_vendor_trust(int(user_id), reason="Dashboard Load")
    if not profile:
        profile = get_or_create_trust_profile(int(user_id))
        
    if not profile:
        return jsonify({"message": "Trust profile not found"}), 404
        
    return jsonify(profile.to_dict()), 200


# ---------------------------------------------------------------------------
# GET /vendor/dashboard-stats — Nudges, views & first-sale stats
# ---------------------------------------------------------------------------

@vendor_bp.route('/dashboard-stats', methods=['GET'])
@jwt_required()
def get_vendor_dashboard_stats_legacy():
    user_id = get_jwt_identity()
    user, sf = _require_vendor_storefront(user_id)
    if not user:
        return jsonify({"message": "Vendor account required"}), 403

    orders = Order.query.filter_by(vendor_id=user.id).all()
    total_orders = len(orders)
    completed_orders = [o for o in orders if o.status in ('COMPLETED', 'PAID', 'DELIVERED', 'SHIPPED', 'IN_ESCROW')]
    total_revenue = sum(float(o.total_amount) for o in completed_orders)

    # First sale check
    is_first_sale = (total_orders == 1)

    # Product views & zero-order nudges
    products = Product.query.filter_by(storefront_id=sf.id, is_active=True, is_deleted=False).all() if sf else []
    total_product_views = sum(p.view_count or 0 for p in products)

    from app.models.order import OrderItem
    nudge_products = []
    for p in products:
        views = p.view_count or 0
        if views >= 3:
            # Check if this product has any order items
            order_item_count = db.session.query(OrderItem).filter_by(product_id=p.id).count()
            if order_item_count == 0:
                nudge_products.append({
                    "id": p.id,
                    "name": p.name,
                    "view_count": views,
                    "price": str(p.price),
                    "image": p.images[0] if p.images else None,
                })

    from app.models.admin import SponsoredListing
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc)
    active_sponsored = SponsoredListing.query.filter(
        SponsoredListing.vendor_id == user.id,
        SponsoredListing.is_active == True,
        SponsoredListing.end_date > now
    ).all()
    sponsored_product_ids = [s.product_id for s in active_sponsored]

    # Calculate dynamic gamified Seller Tier (Jobs-to-be-Done & Identity system)
    completed_count = len(completed_orders)
    products_count = len(products)
    if sf and sf.is_pro_verified and completed_count >= 20:
        seller_tier = {"level": 5, "name": "Elite Seller", "badge": "🏆 Elite Seller", "color": "#f59e0b", "fee_discount": "4.5% fee"}
    elif completed_count >= 5:
        seller_tier = {"level": 4, "name": "Trusted Seller", "badge": "💛 Trusted Seller", "color": "#10b981", "fee_discount": "5.4% fee"}
    elif completed_count >= 1:
        seller_tier = {"level": 3, "name": "Active Seller", "badge": "🔥 Active Seller", "color": "#3b82f6", "fee_discount": "Standard fee"}
    elif products_count >= 1:
        seller_tier = {"level": 2, "name": "Rising Seller", "badge": "⭐ Rising Seller", "color": "#8b5cf6", "fee_discount": "Standard fee"}
    else:
        seller_tier = {"level": 1, "name": "Starter", "badge": "🌱 Starter", "color": "#6b7280", "fee_discount": "Standard fee"}

    return jsonify({
        "store_view_count": sf.view_count if sf else 0,
        "total_product_views": total_product_views,
        "total_orders": total_orders,
        "total_revenue": total_revenue,
        "is_first_sale": is_first_sale,
        "nudge_products": nudge_products,
        "sponsored_product_ids": sponsored_product_ids,
        "seller_tier": seller_tier,
        "pro_verified": {
            "is_pro_verified": bool(sf.is_pro_verified) if sf else False,
            "expires_at": sf.pro_verified_expires_at.isoformat() if (sf and sf.pro_verified_expires_at) else None,
            "verification_status": sf.verification_status if sf else "NOT_SUBMITTED"
        }
    }), 200


# ---------------------------------------------------------------------------
# POST /vendor/pro-verified/checkout — Pay ₦2,500/yr for Pro Verified badge
# ---------------------------------------------------------------------------

@vendor_bp.route('/pro-verified/checkout', methods=['POST'])
@jwt_required()
def checkout_pro_verified():
    user_id = get_jwt_identity()
    user, sf = _require_vendor_storefront(user_id)
    if not user or not sf:
        return jsonify({"message": "Storefront required"}), 400

    from app.services.escrow.paystack_provider import PaystackProvider
    import requests, os

    key = os.environ.get("PAYSTACK_SECRET_KEY", "")
    if not key:
        return jsonify({"message": "Paystack is not configured"}), 400

    txn_ref = f"PRO-VER-{sf.id}-{_uuid.uuid4().hex[:8].upper()}"
    amount_kobo = 250000  # ₦2,500 in kobo

    headers = {
        "Authorization": f"Bearer {key}",
        "Content-Type": "application/json"
    }

    site_url = os.environ.get("SITE_URL", "https://siiqo.com").rstrip('/')
    payload = {
        "email": user.email,
        "amount": amount_kobo,
        "reference": txn_ref,
        "callback_url": f"{site_url}/vendor/settings?pro_verified=success",
        "metadata": {
            "type": "pro_verified_subscription",
            "vendor_id": user.id,
            "storefront_id": sf.id
        }
    }

    try:
        resp = requests.post("https://api.paystack.co/transaction/initialize", json=payload, headers=headers, timeout=15)
        res_data = resp.json()
        if res_data.get("status"):
            return jsonify({
                "status": "success",
                "payment_url": res_data["data"]["authorization_url"],
                "reference": txn_ref,
            }), 200
        return jsonify({"message": res_data.get("message", "Paystack payment init failed")}), 400
    except Exception as e:
        logging.error(f"[PRO VERIFIED CHECKOUT ERR] {e}")
        return jsonify({"message": f"Payment error: {str(e)}"}), 500


# ---------------------------------------------------------------------------
# POST /vendor/products/<int:product_id>/sponsor — Sponsor product (₦2,000/wk)
# ---------------------------------------------------------------------------

@vendor_bp.route('/products/<int:product_id>/sponsor', methods=['POST'])
@jwt_required()
def sponsor_product(product_id):
    user_id = get_jwt_identity()
    user, sf = _require_vendor_storefront(user_id)
    if not user or not sf:
        return jsonify({"message": "Vendor storefront required"}), 400

    product = Product.query.filter_by(id=product_id, storefront_id=sf.id).first()
    if not product:
        return jsonify({"message": "Product not found or not owned by vendor"}), 404

    from app.services.escrow.paystack_provider import PaystackProvider
    import requests, os

    key = os.environ.get("PAYSTACK_SECRET_KEY", "")
    if not key:
        return jsonify({"message": "Paystack is not configured"}), 400

    txn_ref = f"SPONSOR-{product.id}-{_uuid.uuid4().hex[:8].upper()}"
    amount_kobo = 200000  # ₦2,000 in kobo

    headers = {
        "Authorization": f"Bearer {key}",
        "Content-Type": "application/json"
    }

    site_url = os.environ.get("SITE_URL", "https://siiqo.com").rstrip('/')
    payload = {
        "email": user.email,
        "amount": amount_kobo,
        "reference": txn_ref,
        "callback_url": f"{site_url}/vendor/products?sponsored=success",
        "metadata": {
            "type": "sponsored_listing",
            "vendor_id": user.id,
            "product_id": product.id,
        }
    }

    try:
        resp = requests.post("https://api.paystack.co/transaction/initialize", json=payload, headers=headers, timeout=15)
        res_data = resp.json()
        if res_data.get("status"):
            return jsonify({
                "status": "success",
                "payment_url": res_data["data"]["authorization_url"],
                "reference": txn_ref,
            }), 200
        return jsonify({"message": res_data.get("message", "Paystack payment init failed")}), 400
    except Exception as e:
        logging.error(f"[SPONSOR PRODUCT ERR] {e}")
        return jsonify({"message": f"Payment error: {str(e)}"}), 500


@vendor_bp.route('/uploads/presigned-url', methods=['POST'])
@jwt_required()
def get_presigned_upload_url():
    """
    Generates a pre-signed S3 upload payload for fast direct-to-S3 uploads.
    """
    from app.utils.upload import generate_presigned_upload_url
    data = request.get_json() or {}
    filename = data.get('filename')
    file_type = data.get('file_type', 'application/octet-stream')
    subfolder = data.get('subfolder', 'digital_products')
    is_digital = data.get('is_digital', True)

    if not filename:
        return jsonify({"message": "Filename is required"}), 400

    try:
        res = generate_presigned_upload_url(
            filename=filename,
            file_type=file_type,
            subfolder=subfolder,
            is_digital=is_digital
        )
        return jsonify(res), 200
    except ValueError as ve:
        return jsonify({"message": str(ve)}), 400
    except Exception as e:
        logging.error(f"[PRESIGNED S3 ERR] {e}")
        return jsonify({"message": "Failed to generate upload authorization", "is_direct_s3": False}), 500



