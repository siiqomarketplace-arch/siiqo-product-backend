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

    return jsonify({
        "status": "success",
        "store_info": {
            **s.to_public_dict(),
            "whatsapp_link": f"https://wa.me/{s.phone}" if s.phone else None,
        },
        "catalogs": catalogs,
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
            "excerpt": a.excerpt or (a.content[:150] + "..." if len(a.content) > 150 else a.content),
            "cover_image": a.cover_image,
            "created_at": a.created_at.isoformat() if a.created_at else None,
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
