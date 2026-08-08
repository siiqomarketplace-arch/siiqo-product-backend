"""
grants.py — Grants and Funding Opportunities Routes
Handles: Grant CRUD, filtering, categories, featured grants
"""
from flask import Blueprint, request, jsonify
from flask_jwt_extended import jwt_required, get_jwt_identity
from app.extensions import db, limiter
from app.models.grant import Grant
from app.models.admin import AdminUser
from datetime import datetime, timezone, timedelta
from sqlalchemy import or_, and_, func
from app.middleware.admin_auth import admin_required, audit_log

grants_bp = Blueprint('grants', __name__, url_prefix='/api/grants')


def utcnow():
    return datetime.now(timezone.utc)


# ---------------------------------------------------------------------------
# PUBLIC ROUTES — List and view grants
# ---------------------------------------------------------------------------

@grants_bp.route('', methods=['GET'])
@limiter.limit("60 per minute")
def list_grants():
    """
    List grants with filtering and pagination
    
    Query Parameters:
    - page: Page number (default: 1)
    - per_page: Items per page (default: 20, max: 100)
    - status: Filter by status (open, upcoming, closed)
    - country: Filter by country
    - category: Filter by category (can be comma-separated)
    - featured: Filter featured grants (true/false)
    - search: Search in name, description, eligibility
    - sort: Sort order (latest, deadline, amount)
    """
    page = int(request.args.get('page', 1))
    per_page = min(int(request.args.get('per_page', 20)), 100)
    
    # Base query - only published grants
    query = Grant.query.filter_by(is_published=True)
    
    # Filter by status
    status = request.args.get('status')
    if status and status in Grant.get_valid_statuses():
        query = query.filter_by(status=status)
    
    # Filter by country
    country = request.args.get('country')
    if country:
        query = query.filter_by(country=country)
    
    # Filter by category (supports multiple categories)
    category = request.args.get('category')
    if category:
        categories = [c.strip() for c in category.split(',')]
        # Check if grant has ANY of the specified categories
        for cat in categories:
            query = query.filter(Grant.category.any(cat))
    
    # Filter by featured
    featured = request.args.get('featured')
    if featured and featured.lower() == 'true':
        query = query.filter_by(featured=True)
    
    # Search
    search = request.args.get('search')
    if search:
        search_term = f'%{search}%'
        query = query.filter(
            or_(
                Grant.name.ilike(search_term),
                Grant.description.ilike(search_term),
                Grant.eligibility.ilike(search_term)
            )
        )
    
    # Sorting
    sort = request.args.get('sort', 'latest')
    if sort == 'deadline':
        # Sort by deadline (rolling grants at the end)
        query = query.order_by(
            func.case(
                (Grant.deadline == 'Rolling', 2),
                else_=1
            ),
            Grant.deadline.asc()
        )
    elif sort == 'amount':
        # Sort by amount (this is tricky since amount is a string)
        query = query.order_by(Grant.amount.desc())
    else:  # latest (default)
        query = query.order_by(
            Grant.featured.desc(),
            Grant.created_at.desc()
        )
    
    # Paginate
    paginated = query.paginate(page=page, per_page=per_page, error_out=False)
    
    grants = [grant.to_dict() for grant in paginated.items]
    
    return jsonify({
        'grants': grants,
        'total': paginated.total,
        'page': page,
        'per_page': per_page,
        'pages': paginated.pages
    }), 200


@grants_bp.route('/featured', methods=['GET'])
@limiter.limit("60 per minute")
def get_featured_grants():
    """Get featured grants (up to 3)"""
    grants = Grant.query.filter_by(
        featured=True,
        is_published=True
    ).order_by(Grant.created_at.desc()).limit(3).all()
    
    return jsonify({
        'grants': [grant.to_dict() for grant in grants]
    }), 200


@grants_bp.route('/open', methods=['GET'])
@limiter.limit("60 per minute")
def get_open_grants():
    """Get all open grants"""
    page = int(request.args.get('page', 1))
    per_page = min(int(request.args.get('per_page', 20)), 100)
    
    query = Grant.query.filter_by(
        status='open',
        is_published=True
    ).order_by(Grant.featured.desc(), Grant.created_at.desc())
    
    paginated = query.paginate(page=page, per_page=per_page, error_out=False)
    
    return jsonify({
        'grants': [grant.to_dict() for grant in paginated.items],
        'total': paginated.total,
        'page': page,
        'per_page': per_page
    }), 200


@grants_bp.route('/closing-soon', methods=['GET'])
@limiter.limit("60 per minute")
def get_closing_soon_grants():
    """Get grants closing within the next 30 days"""
    page = int(request.args.get('page', 1))
    per_page = min(int(request.args.get('per_page', 20)), 100)
    
    # Calculate 30 days from now
    thirty_days_later = (datetime.now(timezone.utc) + timedelta(days=30)).date()
    today = datetime.now(timezone.utc).date()
    
    # Get all open grants
    all_open = Grant.query.filter_by(
        status='open',
        is_published=True
    ).all()
    
    # Filter those closing within 30 days (exclude "Rolling")
    closing_soon = []
    for grant in all_open:
        if grant.deadline != 'Rolling':
            try:
                deadline_date = datetime.fromisoformat(grant.deadline.replace('Z', '+00:00')).date()
                if today <= deadline_date <= thirty_days_later:
                    closing_soon.append(grant)
            except (ValueError, AttributeError):
                continue
    
    # Sort by deadline
    closing_soon.sort(key=lambda g: g.deadline)
    
    # Manual pagination
    start_idx = (page - 1) * per_page
    end_idx = start_idx + per_page
    page_grants = closing_soon[start_idx:end_idx]
    
    return jsonify({
        'grants': [grant.to_dict() for grant in page_grants],
        'total': len(closing_soon),
        'page': page,
        'per_page': per_page
    }), 200


@grants_bp.route('/upcoming', methods=['GET'])
@limiter.limit("60 per minute")
def get_upcoming_grants():
    """Get upcoming grants"""
    page = int(request.args.get('page', 1))
    per_page = min(int(request.args.get('per_page', 20)), 100)
    
    query = Grant.query.filter_by(
        status='upcoming',
        is_published=True
    ).order_by(Grant.created_at.desc())
    
    paginated = query.paginate(page=page, per_page=per_page, error_out=False)
    
    return jsonify({
        'grants': [grant.to_dict() for grant in paginated.items],
        'total': paginated.total,
        'page': page,
        'per_page': per_page
    }), 200


@grants_bp.route('/category/<category_name>', methods=['GET'])
@limiter.limit("60 per minute")
def get_grants_by_category(category_name):
    """Get grants by category"""
    page = int(request.args.get('page', 1))
    per_page = min(int(request.args.get('per_page', 20)), 100)
    
    # Capitalize category name for consistency
    category_name = category_name.title()
    
    if category_name not in Grant.get_valid_categories():
        return jsonify({'message': f'Invalid category: {category_name}'}), 400
    
    query = Grant.query.filter(
        Grant.category.any(category_name),
        Grant.is_published == True
    ).order_by(Grant.featured.desc(), Grant.created_at.desc())
    
    paginated = query.paginate(page=page, per_page=per_page, error_out=False)
    
    return jsonify({
        'grants': [grant.to_dict() for grant in paginated.items],
        'total': paginated.total,
        'page': page,
        'per_page': per_page,
        'category': category_name
    }), 200


@grants_bp.route('/country/<country_name>', methods=['GET'])
@limiter.limit("60 per minute")
def get_grants_by_country(country_name):
    """Get grants by country"""
    page = int(request.args.get('page', 1))
    per_page = min(int(request.args.get('per_page', 20)), 100)
    
    # Capitalize country name
    country_name = country_name.title()
    
    query = Grant.query.filter_by(
        country=country_name,
        is_published=True
    ).order_by(Grant.featured.desc(), Grant.created_at.desc())
    
    paginated = query.paginate(page=page, per_page=per_page, error_out=False)
    
    return jsonify({
        'grants': [grant.to_dict() for grant in paginated.items],
        'total': paginated.total,
        'page': page,
        'per_page': per_page,
        'country': country_name
    }), 200


@grants_bp.route('/<slug>', methods=['GET'])
@limiter.limit("60 per minute")
def get_grant_by_slug(slug):
    """Get a single grant by slug"""
    grant = Grant.query.filter_by(
        slug=slug,
        is_published=True
    ).first()
    
    if not grant:
        return jsonify({'message': 'Grant not found'}), 404
    
    return jsonify(grant.to_dict()), 200


@grants_bp.route('/stats', methods=['GET'])
@limiter.limit("60 per minute")
def get_grant_stats():
    """Get grant statistics"""
    total = Grant.query.filter_by(is_published=True).count()
    open_count = Grant.query.filter_by(status='open', is_published=True).count()
    upcoming_count = Grant.query.filter_by(status='upcoming', is_published=True).count()
    featured_count = Grant.query.filter_by(featured=True, is_published=True).count()
    
    # Count by country
    countries = db.session.query(
        Grant.country,
        func.count(Grant.id)
    ).filter_by(is_published=True).group_by(Grant.country).all()
    
    return jsonify({
        'total': total,
        'open': open_count,
        'upcoming': upcoming_count,
        'featured': featured_count,
        'by_country': {country: count for country, count in countries}
    }), 200


# ---------------------------------------------------------------------------
# ADMIN ROUTES — Create, Update, Delete grants
# ---------------------------------------------------------------------------

@grants_bp.route('', methods=['POST'])
@admin_required
def create_grant():
    """Create a new grant (admin only)"""
    admin_id = get_jwt_identity()
    data = request.get_json() or {}
    
    # Validate required fields
    required_fields = ['slug', 'name', 'amount', 'category', 'country', 
                      'eligibility', 'description', 'deadline', 'official_url']
    
    for field in required_fields:
        if field not in data:
            return jsonify({'message': f'Missing required field: {field}'}), 400
    
    # Check slug uniqueness
    existing = Grant.query.filter_by(slug=data['slug']).first()
    if existing:
        return jsonify({'message': 'Slug already exists'}), 400
    
    # Validate status
    status = data.get('status', 'upcoming')
    if status not in Grant.get_valid_statuses():
        return jsonify({'message': f'Invalid status: {status}'}), 400
    
    # Create grant
    grant = Grant(
        slug=data['slug'],
        name=data['name'],
        amount=data['amount'],
        category=data['category'],  # Should be a list
        country=data['country'],
        eligibility=data['eligibility'],
        description=data['description'],
        application_tips=data.get('application_tips'),
        deadline=data['deadline'],
        status=status,
        official_url=data['official_url'],
        cover_image=data.get('cover_image'),
        featured=data.get('featured', False),
        is_published=data.get('is_published', True),
        meta_title=data.get('meta_title'),
        meta_description=data.get('meta_description'),
        admin_author_id=admin_id
    )
    
    db.session.add(grant)
    db.session.commit()
    
    # Audit log
    audit_log(admin_id, 'CREATE', 'grant', grant.id, f'Created grant: {grant.name}')
    
    return jsonify({
        'message': 'Grant created successfully',
        'grant': grant.to_dict()
    }), 201


@grants_bp.route('/<int:grant_id>', methods=['PUT'])
@admin_required
def update_grant(grant_id):
    """Update a grant (admin only)"""
    admin_id = get_jwt_identity()
    grant = Grant.query.get(grant_id)
    
    if not grant:
        return jsonify({'message': 'Grant not found'}), 404
    
    data = request.get_json() or {}
    
    # Update fields
    if 'name' in data:
        grant.name = data['name']
    if 'amount' in data:
        grant.amount = data['amount']
    if 'category' in data:
        grant.category = data['category']
    if 'country' in data:
        grant.country = data['country']
    if 'eligibility' in data:
        grant.eligibility = data['eligibility']
    if 'description' in data:
        grant.description = data['description']
    if 'application_tips' in data:
        grant.application_tips = data['application_tips']
    if 'deadline' in data:
        grant.deadline = data['deadline']
    if 'status' in data:
        if data['status'] not in Grant.get_valid_statuses():
            return jsonify({'message': 'Invalid status'}), 400
        grant.status = data['status']
    if 'official_url' in data:
        grant.official_url = data['official_url']
    if 'cover_image' in data:
        grant.cover_image = data['cover_image']
    if 'featured' in data:
        grant.featured = data['featured']
    if 'is_published' in data:
        grant.is_published = data['is_published']
    if 'meta_title' in data:
        grant.meta_title = data['meta_title']
    if 'meta_description' in data:
        grant.meta_description = data['meta_description']
    if 'last_verified' in data:
        grant.last_verified = datetime.fromisoformat(data['last_verified'].replace('Z', '+00:00'))
    
    # Update slug if provided and unique
    if 'slug' in data and data['slug'] != grant.slug:
        existing = Grant.query.filter_by(slug=data['slug']).first()
        if existing:
            return jsonify({'message': 'Slug already exists'}), 400
        grant.slug = data['slug']
    
    db.session.commit()
    
    # Audit log
    audit_log(admin_id, 'UPDATE', 'grant', grant.id, f'Updated grant: {grant.name}')
    
    return jsonify({
        'message': 'Grant updated successfully',
        'grant': grant.to_dict()
    }), 200


@grants_bp.route('/<int:grant_id>', methods=['DELETE'])
@admin_required
def delete_grant(grant_id):
    """Delete a grant (admin only)"""
    admin_id = get_jwt_identity()
    grant = Grant.query.get(grant_id)
    
    if not grant:
        return jsonify({'message': 'Grant not found'}), 404
    
    grant_name = grant.name
    db.session.delete(grant)
    db.session.commit()
    
    # Audit log
    audit_log(admin_id, 'DELETE', 'grant', grant_id, f'Deleted grant: {grant_name}')
    
    return jsonify({'message': 'Grant deleted successfully'}), 200


# ---------------------------------------------------------------------------
# UTILITY ROUTES
# ---------------------------------------------------------------------------

@grants_bp.route('/categories', methods=['GET'])
@limiter.limit("60 per minute")
def get_valid_categories():
    """Get list of valid grant categories"""
    return jsonify({
        'categories': Grant.get_valid_categories()
    }), 200


@grants_bp.route('/countries', methods=['GET'])
@limiter.limit("60 per minute")
def get_valid_countries():
    """Get list of valid countries"""
    return jsonify({
        'countries': Grant.get_valid_countries()
    }), 200
