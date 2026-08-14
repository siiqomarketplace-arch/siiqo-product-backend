import logging
"""
public.py â€” Public marketplace routes (no auth required)
"""
from flask import Blueprint, request, jsonify
from app.extensions import db, limiter
from app.models.product import Product, Category
from app.models.user import Storefront, User
from app.models.community import Article, Review
from app.models.admin import SponsoredListing

public_bp = Blueprint('public', __name__)


# ---------------------------------------------------------------------------
# GET /marketplace/products
# ---------------------------------------------------------------------------

@public_bp.route('/products', methods=['GET'])
def get_products():
    city = (request.args.get('city') or '').strip()
    category_slug = (request.args.get('category') or '').strip()
    search_q = (request.args.get('q') or '').strip()
    product_type = (request.args.get('product_type') or '').strip()
    page = int(request.args.get('page', 1))
    per_page = min(int(request.args.get('per_page', 24)), 100)

    query = (
        Product.query
        .join(Storefront)
        .filter(
            Product.is_active == True,
            Product.stock_quantity > 0,
            Storefront.is_verified == True,
            Storefront.is_published == True,
        )
    )

    if search_q:
        query = query.filter(
            Product.name.ilike(f'%{search_q}%') |
            Product.description.ilike(f'%{search_q}%')
        )

    if category_slug:
        from app.models.product import Category as Cat
        cat = Cat.query.filter_by(slug=category_slug).first()
        if cat:
            query = query.filter(Product.category_id == cat.id)

    if product_type:
        types = [t.strip().lower() for t in product_type.split(',')]
        if 'physical' in types:
            # Physical includes 'physical', None, and empty strings since it is the default
            query = query.filter((Product.product_type == 'physical') | (Product.product_type == None) | (Product.product_type == ''))
        else:
            query = query.filter(Product.product_type.in_(types))

    # Hyper-local: products from buyer's city come first
    if city:
        query = query.order_by(
            db.case((Storefront.city.ilike(city), 0), else_=1),
            Product.created_at.desc()
        )
    else:
        query = query.order_by(Product.created_at.desc())

    paginated = query.paginate(page=page, per_page=per_page, error_out=False)

    # Sponsored listings (inject at top)
    sponsored_ids = set()
    if page == 1:
        sponsored = (
            SponsoredListing.query
            .filter_by(is_active=True)
            .order_by(SponsoredListing.amount_paid.desc())
            .limit(4)
            .all()
        )
        sponsored_ids = {s.product_id for s in sponsored}
        for s in sponsored:
            s.impressions = (s.impressions or 0) + 1
        if sponsored:
            db.session.commit()

    def _product_dict(p, is_sponsored=False):
        sf = p.storefront
        # Compute rating from reviews (use .count() since lazy='dynamic')
        try:
            approved_reviews = p.reviews.filter_by(is_approved=True).all()
            ratings = [r.vendor_rating for r in approved_reviews]
            avg_rating = round(sum(ratings) / len(ratings), 1) if ratings else None
            review_count = len(ratings)
        except Exception:
            avg_rating = None
            review_count = 0
        return {
            "id": p.id,
            "name": p.name,
            "price": str(p.price),
            "description": p.description,
            "image": (p.images[0] if p.images else None),
            "images": p.images or [],
            "stock_quantity": p.stock_quantity,
            "storefront_id": p.storefront_id,
            "storefront": sf.store_name if sf else None,
            "storefront_slug": sf.store_slug if sf else None,
            "trust_score": sf.vendor.trust_score_or_default if sf and sf.vendor else 500,
            "trust_tier": sf.vendor.trust_tier_or_default if sf and sf.vendor else 'SILVER',
            # â”€â”€ vendor identity (required for chat / messaging) â”€â”€
            "vendor_id": sf.vendor_id if sf else None,
            "user_id": sf.vendor_id if sf else None,
            "vendor_name": sf.store_name if sf else None,
            # â”€â”€ contact details â”€â”€
            "vendor_phone": sf.phone if sf else None,
            "whatsapp_link": (f"https://wa.me/{sf.phone}" if sf and sf.phone else None),
            "city": sf.city if sf else None,
            "state": sf.state if sf else None,
            "category_id": p.category_id,
            "category": p.category.name if p.category else "General",
            "is_negotiable": p.is_negotiable,
            "is_sponsored": is_sponsored,
            # â”€â”€ ratings (now included in listing so marketplace cards show real stars) â”€â”€
            "avg_rating": avg_rating,
            "rating": avg_rating,         # alias â€” frontend reads either key
            "review_count": review_count,
            "condition": p.condition,
            "location": p.location,
            "latitude": p.latitude,
            "longitude": p.longitude,
            "product_type": p.product_type or "physical",
            "file_url": p.file_url,
            "booking_link": p.booking_link,
            # Category-specific attributes â€” null for old listings, safe to show
            "attributes": p.attributes or {},
        }

    products = [_product_dict(p, p.id in sponsored_ids) for p in paginated.items]

    return jsonify({
        "products": products,
        "total": paginated.total,
        "page": page,
        "per_page": per_page,
        "pages": paginated.pages,
        "has_next": paginated.has_next,
    }), 200


# ---------------------------------------------------------------------------
# GET /marketplace/products/<id>
# ---------------------------------------------------------------------------

@public_bp.route('/products/<int:product_id>', methods=['GET'])
def get_product_details(product_id):
    p = db.session.get(Product, product_id)
    if not p or not p.is_active:
        return jsonify({"message": "Product not found"}), 404

    # Increment view_count on fetch
    try:
        p.view_count = (p.view_count or 0) + 1
        db.session.commit()
    except Exception:
        db.session.rollback()

    # Average rating â€” p.reviews is a dynamic relationship, use query
    avg_rating = None
    review_count = 0
    try:
        approved = p.reviews.filter_by(is_approved=True).all()
        ratings = [r.vendor_rating for r in approved]
        if ratings:
            avg_rating = round(sum(ratings) / len(ratings), 1)
            review_count = len(ratings)
    except Exception:
        pass

    return jsonify({
        "id": p.id,
        "name": p.name,
        "price": str(p.price),
        "description": p.description,
        "images": p.images or [],
        "stock_quantity": p.stock_quantity,
        "category_id": p.category_id,
        "condition": p.condition,
        "location": p.location,
        "latitude": p.latitude,
        "longitude": p.longitude,
        "avg_rating": avg_rating,
        "review_count": review_count,
        "is_negotiable": p.is_negotiable,
        "product_type": p.product_type,
        "file_url": p.file_url,
        "booking_link": p.booking_link,
        "sku": p.sku,
        "weight": str(p.weight) if p.weight else None,
        "seo_title": p.seo_title,
        "seo_description": p.seo_description,
        "created_at": p.created_at.isoformat() + "Z" if p.created_at else None,
        "updated_at": p.updated_at.isoformat() + "Z" if p.updated_at else None,
        # Category-specific attributes â€” null for old listings, safe to show
        "attributes": p.attributes or {},
        "storefront": {
            "id": p.storefront.id,
            "vendor_id": p.storefront.vendor_id,  # Added for chat functionality
            "name": p.storefront.store_name,
            "slug": p.storefront.store_slug,
            "logo": p.storefront.store_logo,
            "city": p.storefront.city,
            "state": p.storefront.state,
            "phone": p.storefront.phone,
            "social_links": p.storefront.social_links or {},
            "trust_score": p.storefront.vendor.trust_score_or_default if p.storefront.vendor else 500,
            "trust_tier": p.storefront.vendor.trust_tier_or_default if p.storefront.vendor else 'SILVER',
        } if p.storefront else None,
    }), 200


# ---------------------------------------------------------------------------
# GET /marketplace/storefronts  â€” only live stores
# ---------------------------------------------------------------------------

@public_bp.route('/storefronts', methods=['GET'])
def get_storefronts():
    city = (request.args.get('city') or '').strip()
    page = int(request.args.get('page', 1))
    per_page = min(int(request.args.get('per_page', 20)), 100)

    query = Storefront.query.filter_by(is_verified=True, is_published=True)

    if city:
        query = query.order_by(
            db.case((Storefront.city.ilike(city), 0), else_=1),
            Storefront.created_at.desc()
        )
    else:
        query = query.order_by(Storefront.created_at.desc())

    paginated = query.paginate(page=page, per_page=per_page, error_out=False)

    return jsonify({
        "storefronts": [s.to_public_dict() for s in paginated.items],
        "total": paginated.total,
        "page": page,
        "pages": paginated.pages,
    }), 200


# ---------------------------------------------------------------------------
# GET /marketplace/store/<slug>
# ---------------------------------------------------------------------------

@public_bp.route('/store/<string:slug>', methods=['GET'])
def get_storefront_details(slug):
    s = Storefront.query.filter_by(store_slug=slug).first()
    if not s:
        return jsonify({"message": "Store not found"}), 404

    if not s.is_verified:
        return jsonify({
            "status": "pending_approval",
            "message": "This storefront is under review. It will go live once approved.",
        }), 202

    if not s.is_published:
        return jsonify({
            "status": "offline",
            "message": "This storefront is currently offline.",
        }), 202

    # Increment store view_count on public fetch
    try:
        s.view_count = (s.view_count or 0) + 1
        db.session.commit()
    except Exception:
        db.session.rollback()

    products = Product.query.filter(
        Product.storefront_id == s.id,
        Product.is_active == True,
        Product.stock_quantity > 0
    ).limit(500).all()

    # Group by category
    from collections import defaultdict
    by_category: dict = defaultdict(list)
    for p in products:
        cat_name = p.category.name if p.category else "All Products"
        by_category[cat_name].append({
            "id": p.id,
            "name": p.name,
            "price": str(p.price),
            "images": p.images or [],
            "description": p.description,
            "stock_quantity": p.stock_quantity,
            "condition": p.condition,
            "location": p.location,
            "latitude": p.latitude,
            "longitude": p.longitude,
            "is_negotiable": p.is_negotiable,
            "product_type": p.product_type or "physical",
            "file_url": p.file_url,
            "booking_link": p.booking_link,
        })

    catalogs = [
        {"catalog_name": cat, "products": prods}
        for cat, prods in by_category.items()
    ]

    # Fetch published active events for this storefront (where show_on_storefront is True)
    events_data = []
    try:
        from app.models.event import Event
        events = Event.query.filter(
            Event.storefront_id == s.id,
            Event.is_active == True,
            Event.is_published == True,
            Event.is_deleted == False
        ).all()
        # Filter show_on_storefront (default True)
        events_data = [
            e.to_dict(include_ticket_types=True) 
            for e in events 
            if getattr(e, 'show_on_storefront', True) is not False
        ]
    except Exception as ev_err:
        logger.error(f"Error fetching storefront events: {ev_err}")

    return jsonify({
        "status": "success",
        "store_info": {
            **s.to_public_dict(),
            "whatsapp_link": f"https://wa.me/{s.phone}" if s.phone else None,
        },
        "catalogs": catalogs,
        "events": events_data,
        "product_count": len(products),
    }), 200


# ---------------------------------------------------------------------------
# GET /marketplace/search
# ---------------------------------------------------------------------------

@public_bp.route('/search', methods=['GET'])
@limiter.limit("30 per minute")
def search():
    q = (request.args.get('q') or '').strip()
    if not q or len(q) < 2:
        return jsonify({"products": [], "storefronts": []}), 200

    products = (
        Product.query
        .join(Storefront)
        .filter(
            Product.is_active == True,
            Product.stock_quantity > 0,
            Storefront.is_verified == True,
            Storefront.is_published == True,
            Product.name.ilike(f'%{q}%') | Product.description.ilike(f'%{q}%')
        )
        .limit(20)
        .all()
    )

    storefronts = (
        Storefront.query
        .filter(
            Storefront.is_verified == True,
            Storefront.is_published == True,
            Storefront.store_name.ilike(f'%{q}%') | Storefront.store_description.ilike(f'%{q}%')
        )
        .limit(10)
        .all()
    )

    return jsonify({
        "products": [{
            "id": p.id,
            "name": p.name,
            "price": str(p.price),
            "images": p.images or [],
            "storefront": p.storefront.store_name if p.storefront else None,
            "storefront_slug": p.storefront.store_slug if p.storefront else None,
            "condition": p.condition,
            "location": p.location,
            "latitude": p.latitude,
            "longitude": p.longitude,
        } for p in products],
        "storefronts": [s.to_public_dict() for s in storefronts],
    }), 200


# ---------------------------------------------------------------------------
# GET /marketplace/categories
# ---------------------------------------------------------------------------

@public_bp.route('/categories', methods=['GET'])
def get_categories():
    cats = Category.query.all()
    if cats:
        return jsonify([{
            "id": c.id,
            "name": c.name,
            "slug": c.slug,
            # New fields â€” null for old categories, schema for enriched ones
            "icon": c.icon,
            "attribute_schema": c.attribute_schema or [],
            "product_type_hint": c.product_type_hint or [],
        } for c in cats]), 200

    # Seed defaults if DB is empty â€” now with icons included
    return jsonify([
        {"id": 1, "name": "Electronics", "slug": "electronics", "icon": "Cpu", "attribute_schema": [], "product_type_hint": ["physical"]},
        {"id": 2, "name": "Fashion", "slug": "fashion", "icon": "Shirt", "attribute_schema": [], "product_type_hint": ["physical"]},
        {"id": 3, "name": "Home & Furniture", "slug": "home-furniture", "icon": "Home", "attribute_schema": [], "product_type_hint": ["physical"]},
        {"id": 4, "name": "Beauty", "slug": "beauty", "icon": "Sparkles", "attribute_schema": [], "product_type_hint": ["physical"]},
        {"id": 5, "name": "Food & Drinks", "slug": "food-drinks", "icon": "UtensilsCrossed", "attribute_schema": [], "product_type_hint": ["physical"]},
        {"id": 6, "name": "Services", "slug": "services", "icon": "Briefcase", "attribute_schema": [], "product_type_hint": ["service"]},
        {"id": 7, "name": "Health", "slug": "health", "icon": "Heart", "attribute_schema": [], "product_type_hint": ["physical"]},
        {"id": 8, "name": "Sports", "slug": "sports", "icon": "Dumbbell", "attribute_schema": [], "product_type_hint": ["physical"]},
    ]), 200


# ---------------------------------------------------------------------------
# Blog
# ---------------------------------------------------------------------------

@public_bp.route('/blog', methods=['GET'])
def get_articles():
    page = int(request.args.get('page', 1))
    per_page = min(int(request.args.get('per_page', 10)), 50)
    category = request.args.get('category')
    
    query = Article.query.filter_by(is_published=True)
    if category:
        query = query.filter_by(category=category)
        
    paginated = (
        query
        .order_by(Article.created_at.desc())
        .paginate(page=page, per_page=per_page, error_out=False)
    )
    return jsonify({
        "articles": [{
            "id": a.id,
            "title": a.title,
            "slug": a.slug,
            "category": a.category,
            "excerpt": a.excerpt or (a.content[:150] + "..." if a.content and len(a.content) > 150 else (a.content or "")),
            "cover_image": a.cover_image,
            "created_at": a.created_at.isoformat() if a.created_at else None,
            # Calculate read time from full content so cards show accurate time
            "read_time": max(1, round(len((a.content or "").split()) / 200)) if a.content else None,
        } for a in paginated.items],
        "total": paginated.total,
        "pages": paginated.pages,
    }), 200

@public_bp.route('/blog/<string:slug>', methods=['GET'])
def get_article_by_slug(slug):
    a = Article.query.filter_by(slug=slug, is_published=True).first()
    if not a:
        return jsonify({"message": "Article not found"}), 404
    return jsonify({
        "id": a.id,
        "title": a.title,
        "category": a.category,
        "content": a.content,
        "excerpt": a.excerpt,
        "cover_image": a.cover_image,
        "meta_title": a.meta_title or a.title,
        "meta_description": a.meta_description or a.excerpt,
        "author": a.admin_author.name if a.admin_author else "Siiqo Team",
        "created_at": a.created_at.isoformat() if a.created_at else None,
    }), 200


# ---------------------------------------------------------------------------
# Reviews (public read)
# ---------------------------------------------------------------------------

@public_bp.route('/reviews/<int:vendor_id>', methods=['GET'])
def get_vendor_reviews(vendor_id):
    reviews = (
        Review.query
        .filter_by(vendor_id=vendor_id, is_approved=True)
        .order_by(Review.created_at.desc())
        .limit(20)
        .all()
    )
    avg = None
    if reviews:
        avg = round(sum(r.vendor_rating for r in reviews) / len(reviews), 1)

    return jsonify({
        "vendor_id": vendor_id,
        "average_rating": avg,
        "review_count": len(reviews),
        "reviews": [{
            "id": r.id,
            "rating": r.vendor_rating,
            "text": r.review_text,
            "buyer_name": r.buyer.full_name if r.buyer else "Anonymous",
            "created_at": r.created_at.isoformat() if r.created_at else None,
        } for r in reviews],
    }), 200


# ---------------------------------------------------------------------------
# GET /marketplace/vendor/<vendor_id>/trust-breakdown  â€” public trust detail
# ---------------------------------------------------------------------------

@public_bp.route('/vendor/<int:vendor_id>/trust-breakdown', methods=['GET'])
def get_vendor_trust_breakdown(vendor_id):
    """
    Public endpoint â€” returns a vendor's trust score breakdown.
    Only non-private information is included (no financial or personal data).
    """
    vendor = User.query.get(vendor_id)
    if not vendor:
        return jsonify({"message": "Vendor not found"}), 404

    # Use the same computed property as product listings
    total_score = vendor.trust_score_or_default
    tier = vendor.trust_tier_or_default

    # Attempt to get the detailed profile (may not exist for all vendors)
    profile = None
    try:
        from app.models.trust import TrustProfile
        profile = TrustProfile.query.filter_by(vendor_id=vendor_id).first()
    except Exception:
        pass

    if profile:
        pillars = [
            {
                "label": "Completion",
                "score": int(getattr(profile, 'completion_score', 0) or 0),
                "max": 200,
            },
            {
                "label": "Satisfaction",
                "score": int(getattr(profile, 'satisfaction_score', 0) or 0),
                "max": 200,
            },
            {
                "label": "Responsiveness",
                "score": int(getattr(profile, 'responsiveness_score', 0) or 0),
                "max": 200,
            },
            {
                "label": "Compliance",
                "score": int(getattr(profile, 'compliance_score', 0) or 0),
                "max": 200,
            },
            {
                "label": "Community",
                "score": int(getattr(profile, 'community_score', 0) or 0),
                "max": 200,
            },
        ]
    else:
        # Distribute total score proportionally across 5 pillars (max 200 each)
        base = total_score // 5
        remainder = total_score - (base * 5)
        pillars = [
            {"label": "Completion", "score": base, "max": 200},
            {"label": "Satisfaction", "score": base, "max": 200},
            {"label": "Responsiveness", "score": base, "max": 200},
            {"label": "Compliance", "score": base + remainder, "max": 200},
            {"label": "Community", "score": base, "max": 200},
        ]

    storefront = vendor.storefront if hasattr(vendor, 'storefront') else None

    return jsonify({
        "vendor_id": vendor_id,
        "tier": tier,
        "total_score": total_score,
        "pillars": pillars,
        "verified": {
            "email": bool(vendor.is_verified),
            "phone": bool(vendor.phone),
            "bank_account": bool(getattr(vendor, 'bank_account', None) or getattr(vendor, 'account_number', None)),
            "cac_registered": bool(getattr(vendor, 'cac_number', None)),
            "id_verified": bool(vendor.nin),
        },
    }), 200



# ---------------------------------------------------------------------------
# DIGITAL PRODUCT DOWNLOADS
# ---------------------------------------------------------------------------

@public_bp.route('/orders/<int:order_id>/downloads', methods=['GET'])
def get_order_downloads(order_id):
    """
    Get download links for digital products in a completed order
    No auth required - order ID acts as access token
    """
    from app.models.order import Order
    from flask_jwt_extended import jwt_required, get_jwt_identity
    
    # Optional auth - if user is logged in, verify they own the order
    try:
        from flask_jwt_extended import verify_jwt_in_request
        verify_jwt_in_request(optional=True)
        user_id = get_jwt_identity()
        
        if user_id:
            order = Order.query.filter_by(id=order_id, buyer_id=int(user_id)).first()
        else:
            order = Order.query.filter_by(id=order_id).first()
    except:
        order = Order.query.filter_by(id=order_id).first()
    
    if not order:
        return jsonify({'message': 'Order not found'}), 404
    
    # Only show downloads for completed/delivered orders
    if order.status not in ['DELIVERED', 'COMPLETED']:
        return jsonify({'message': 'Order not completed yet'}), 400
    
    # Get digital products from order
    downloads = []
    for item in order.items:
        if item.product and item.product.product_type == 'digital' and item.product.file_url:
            downloads.append({
                'product_id': item.product.id,
                'product_name': item.product.name,
                'download_url': item.product.file_url,
                'image': item.product.images[0] if item.product.images else None
            })
    
    if not downloads:
        return jsonify({'message': 'No digital products in this order'}), 404
    
    return jsonify({
        'order_id': order.id,
        'downloads': downloads
    }), 200


@public_bp.route('/products/<int:product_id>/claim-free', methods=['POST'])
def claim_free_product(product_id):
    """
    Claim a free digital product or service
    Creates order and immediately provides access
    Requires authentication
    """
    from flask_jwt_extended import jwt_required, get_jwt_identity
    from app.models.order import Order, OrderItem
    from app.models.communication import Notification
    from datetime import datetime
    
    try:
        from flask_jwt_extended import verify_jwt_in_request
        verify_jwt_in_request()
        user_id = get_jwt_identity()
    except:
        return jsonify({'message': 'Authentication required to claim free products'}), 401
    
    user = db.session.get(User, int(user_id))
    if not user:
        return jsonify({'message': 'User not found'}), 404
    
    product = Product.query.filter(
        Product.id == product_id,
        Product.is_active == True,
        db.or_(Product.is_free == True, Product.price == 0)
    ).first()
    
    if not product:
        return jsonify({'message': 'Free product not found'}), 404
    
    # Auto-ensure is_free flag is updated if price is 0
    if product.price == 0 and not product.is_free:
        product.is_free = True
        db.session.commit()
    
    p_type = product.product_type or ('digital' if product.file_url else 'service' if product.booking_link else 'digital')
    
    # Check if user already claimed this product
    existing_order = Order.query.join(OrderItem).filter(
        Order.buyer_id == user.id,
        OrderItem.product_id == product.id,
        Order.total_amount == 0
    ).first()
    
    if existing_order:
        file_url = product.file_url or product.booking_link
        return jsonify({
            'message': 'You already claimed this product',
            'order_id': existing_order.id,
            'download_url': file_url,
            'booking_url': product.booking_link or product.file_url,
            'file_url': file_url,
            'access_type': 'booking' if product.product_type == 'service' else 'download',
            'access_url': file_url,
        }), 200
    
    # Create free order
    order = Order(
        buyer_id=user.id,
        vendor_id=product.storefront.vendor_id,
        total_amount=0,
        status='COMPLETED',  # Free products are instantly completed
        payment_method='FREE'
    )
    db.session.add(order)
    db.session.flush()
    
    # Create order item
    order_item = OrderItem(
        order_id=order.id,
        product_id=product.id,
        price_at_purchase=0,
        quantity=1
    )
    db.session.add(order_item)
    
    # Send notification to buyer
    notification = Notification(
        user_id=user.id,
        title=f'Free {product.product_type.title()} Claimed! 🎉',
        message=f'You now have access to: {product.name}',
        type='ORDER',
        order_id=order.id
    )
    db.session.add(notification)
    
    db.session.commit()

    logging.info(f"Free product claimed: {product.id} by user {user.id}")

    raw_file_url = product.file_url or product.booking_link
    raw_booking_url = product.booking_link or product.file_url

    def _make_abs(u):
        if not u:
            return u
        if u.startswith('/static/'):
            # Convert /static/uploads/... to absolute URL
            base = request.host_url.rstrip('/')
            return f"{base}{u}"
        return u

    file_url = _make_abs(raw_file_url)
    booking_url = _make_abs(raw_booking_url)

    # ── Send emails (background threads, never block response) ────────────────
    try:
        from app.utils.email import send_siiqo_email
        from datetime import datetime, timezone

        buyer_name = user.full_name or (user.email.split('@')[0] if user.email else 'there')
        claimed_at = datetime.now(timezone.utc).strftime('%d %b %Y, %H:%M UTC')

        # 1. Email to buyer with download link
        send_siiqo_email(
            to_email=user.email,
            subject=f'📥 Your Free Download is Ready — {product.name}',
            template_name='free_download_buyer',
            buyer_name=buyer_name,
            product_name=product.name,
            vendor_name=product.storefront.store_name if product.storefront else None,
            order_id=order.id,
            download_url=file_url,
        )

        # 2. Email to vendor notifying them of the download
        vendor = product.storefront.vendor if product.storefront else None
        if vendor and vendor.email:
            send_siiqo_email(
                to_email=vendor.email,
                subject=f'📥 Someone Downloaded Your Free Product — {product.name}',
                template_name='free_download_vendor',
                vendor_name=product.storefront.store_name or vendor.full_name or 'Vendor',
                product_name=product.name,
                buyer_name=buyer_name,
                order_id=order.id,
                claimed_at=claimed_at,
            )
    except Exception as email_err:
        logging.warning(f"[CLAIM_FREE] Email dispatch failed (non-fatal): {email_err}")

    # Return access information
    response = {
        'message': 'Free product claimed successfully!',
        'order_id': order.id,
        'product': {
            'id': product.id,
            'name': product.name,
            'type': product.product_type,
        },
        'download_url': file_url,
        'booking_url': booking_url,
        'file_url': file_url,
        'access_type': 'booking' if product.product_type == 'service' else 'download',
    }

    return jsonify(response), 201
