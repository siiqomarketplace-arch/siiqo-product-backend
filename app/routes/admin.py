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
from app.models.admin import AdminUser, PlatformSetting, SubscriptionPlan
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
            "has_storefront": u.storefront is not None,
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
    orders = Order.query.filter(
        (Order.buyer_id == user.id) | (Order.vendor_id == user.id)
    ).count()

    return jsonify({
        "user": {
            **user.to_public_dict(),
            "total_orders": orders,
            "storefront": user.storefront.to_public_dict() if user.storefront else None,
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
        user.is_verified = True
        user.is_active = True
        if user.storefront:
            user.storefront.is_verified = True
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
    elif status == 'unverified':
        user.is_verified = False

    db.session.commit()
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

    storefronts = Storefront.query.order_by(Storefront.created_at.desc()).limit(500).all()
    return jsonify({
        "storefronts": [{
            "id": s.id,
            "business_name": s.store_name,
            "store_slug": s.store_slug,
            "vendor_name": s.vendor.full_name if s.vendor else "Unknown",
            "vendor_email": s.vendor.email if s.vendor else "",
            "is_verified": s.is_verified,
            "is_published": s.is_published,
            "is_live": s.is_live,
            "vendor_status": "Approved" if s.is_verified else "Pending",
            "city": s.city,
            "state": s.state,
            "created_at": s.created_at.isoformat() if s.created_at else None,
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
            "categories": [{"id": c.id, "name": c.name, "slug": c.slug} for c in cats],
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

    cat = Category(name=name, slug=slug)
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

    pending = EscrowTransaction.query.filter(
        EscrowTransaction.status.in_([
            EscrowStatus.PENDING_PAYMENT,
            EscrowStatus.IN_ESCROW,
            EscrowStatus.DISPUTED,
        ])
    ).order_by(EscrowTransaction.created_at.desc()).limit(500).all()

    return jsonify({
        "pending_escrow": [e.to_dict() for e in pending],
        "count": len(pending),
    }), 200


@admin_bp.route('/escrow/verify/<int:order_id>', methods=['POST'])
@jwt_required()
def admin_verify_payment(order_id):
    """Admin manually marks a payment as received (for bank transfer cases)."""
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

    db.session.commit()
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
            "status": p.status,
            "state_of_operation": p.state_of_operation,
            "applied_at": p.applied_at.isoformat() if p.applied_at else None,
            "applicant": {
                "name": p.user.full_name if p.user else "Unknown",
                "email": p.user.email if p.user else "",
            },
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
            upload_dir = os.path.join('uploads', 'blog')
            os.makedirs(upload_dir, exist_ok=True)
            ext = cover_image_file.filename.rsplit('.', 1)[-1] if '.' in cover_image_file.filename else 'jpg'
            filename = f"{uuid.uuid4().hex}.{ext}"
            filepath = os.path.join(upload_dir, filename)
            cover_image_file.save(filepath)
            cover_image_url = f"/uploads/blog/{filename}"
    else:
        data = request.get_json() or {}
        cover_image_url = data.get('cover_image')

    title = data.get('title', '')
    if not title:
        return jsonify({"message": "Title is required"}), 400
    slug = data.get('slug') or re.sub(r'[^a-z0-9]+', '-', title.lower()).strip('-')

    new_article = Article(
        admin_author_id=parsed_id,
        title=title,
        slug=slug,
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
            upload_dir = os.path.join('uploads', 'blog')
            os.makedirs(upload_dir, exist_ok=True)
            ext = cover_image_file.filename.rsplit('.', 1)[-1] if '.' in cover_image_file.filename else 'jpg'
            filename = f"{uuid.uuid4().hex}.{ext}"
            filepath = os.path.join(upload_dir, filename)
            cover_image_file.save(filepath)
            article.cover_image = f"/uploads/blog/{filename}"
    else:
        data = request.get_json() or {}
        if 'cover_image' in data and isinstance(data['cover_image'], str):
            article.cover_image = data['cover_image']

    if 'title' in data and data['title']:
        article.title = data['title']
        if not data.get('slug'):
            article.slug = re.sub(r'[^a-z0-9]+', '-', data['title'].lower()).strip('-')
    if 'slug' in data and data['slug']:
        article.slug = data['slug']
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
            unsubscribe_link = f"{base_url}/unsubscribe?email={user.email}&token={token}"
            
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
