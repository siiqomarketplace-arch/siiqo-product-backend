"""Trust Passport API (free for sellers; platforms read it like a plugin)."""
import jwt
from flask import Blueprint, Response, jsonify, request
from flask_jwt_extended import get_jwt_identity, jwt_required

from app.extensions import db, limiter
from app.services import passport as svc

passport_bp = Blueprint('passport', __name__)


def _rate_key():
    # Platforms with an API key get their own, larger bucket.
    raw = request.headers.get('X-Siiqo-Api-Key', '')
    return f"key:{raw[:12]}" if raw else f"ip:{request.remote_addr}"


def _rate_limit():
    return "600 per minute" if request.headers.get('X-Siiqo-Api-Key') else "60 per minute"


def _public_passport(slug):
    sf = svc.find_storefront(slug)
    if not svc.is_visible(sf):
        return None
    return svc.build_passport(sf)


@passport_bp.route('/.well-known/jwks.json', methods=['GET'])
def get_jwks():
    return jsonify(svc.jwks()), 200


@passport_bp.route('/verify', methods=['POST'])
@limiter.limit(_rate_limit, key_func=_rate_key)
def verify_statement():
    token = ((request.get_json() or {}).get('statement') or '').strip()
    if not token:
        return jsonify({"valid": False, "message": "statement is required"}), 400
    try:
        claims, revoked = svc.verify(token)
    except jwt.ExpiredSignatureError:
        return jsonify({"valid": False, "reason": "expired"}), 200
    except jwt.InvalidTokenError:
        return jsonify({"valid": False, "reason": "invalid"}), 200
    return jsonify({"valid": not revoked, "revoked": revoked, "claims": claims}), 200


@passport_bp.route('/me', methods=['GET', 'PUT'])
@jwt_required()
def my_passport():
    """The seller's own Passport and sharing choices."""
    from app.models.user import Storefront
    try:
        user_id = int(get_jwt_identity())
    except (TypeError, ValueError):
        return jsonify({"message": "Seller account required"}), 403
    sf = Storefront.query.filter_by(vendor_id=user_id).first()
    if not sf:
        return jsonify({"message": "Create your store to get your Trust Passport."}), 404
    settings = svc.settings_for(user_id, create=True)
    if settings.claimed_at is None:
        settings.claimed_at = svc._now()
    if request.method == 'PUT':
        data = request.get_json() or {}
        for field in ('is_public', 'show_completed_orders', 'show_member_since'):
            if field in data:
                setattr(settings, field, bool(data[field]))
    db.session.commit()
    return jsonify({
        "passport": svc.build_passport(sf),
        "settings": {
            "is_public": settings.is_public,
            "show_completed_orders": settings.show_completed_orders,
            "show_member_since": settings.show_member_since,
        },
        "share_url": f"https://siiqo.com/id/{sf.store_slug}",
        "badge_url": f"https://api.siiqo.com/api/passport/{sf.store_slug}/badge.svg",
    }), 200


@passport_bp.route('/<slug>', methods=['GET'])
@limiter.limit(_rate_limit, key_func=_rate_key)
def get_passport(slug):
    raw_key = request.headers.get('X-Siiqo-Api-Key')
    if raw_key and not svc.client_for_key(raw_key):
        return jsonify({"message": "Invalid API key"}), 401
    claims = _public_passport(slug)
    if claims is None:
        return jsonify({"message": "No public Trust Passport for this seller."}), 404
    statement, alg = svc.sign(claims)
    return jsonify({"passport": claims, "statement": statement, "alg": alg}), 200


@passport_bp.route('/<slug>/badge.svg', methods=['GET'])
@limiter.limit("120 per minute")
def get_badge(slug):
    claims = _public_passport(slug)
    if claims is None:
        return Response(status=404)
    res = Response(svc.badge_svg(claims), mimetype='image/svg+xml')
    res.headers['Cache-Control'] = 'public, max-age=900'
    return res


@passport_bp.route('/clients', methods=['POST'])
@jwt_required()
def create_client():
    """Superadmin: issue an API key for a platform. The key is shown once."""
    from app.routes.admin import _parse_admin_id, _require_superadmin
    try:
        admin = _require_superadmin(_parse_admin_id(get_jwt_identity()))
    except (TypeError, ValueError):
        admin = None
    if not admin:
        return jsonify({"message": "Superadmin access required"}), 403
    data = request.get_json() or {}
    name = (data.get('name') or '').strip()
    if not name:
        return jsonify({"message": "name is required"}), 400
    client, raw = svc.create_api_client(name, plan=data.get('plan', 'free'))
    return jsonify({"id": client.id, "name": client.name, "api_key": raw,
                    "message": "Store this key now; it won't be shown again."}), 201


# ── self-serve developer keys (developers portal) ──────────────────────────

def _signed_in_user():
    """The signed-in Siiqo user (any role), or None for admins and unknown ids."""
    from app.models.user import User
    identity = get_jwt_identity()
    try:
        user_id = int(identity)
    except (TypeError, ValueError):
        return None
    user = db.session.get(User, user_id)
    return user if user is not None and user.is_active else None


@passport_bp.route('/keys', methods=['GET', 'POST'])
@jwt_required()
@limiter.limit("30 per minute")
def developer_keys():
    user = _signed_in_user()
    if user is None:
        return jsonify({"message": "Sign in with a Siiqo account to manage API keys."}), 403

    if request.method == 'GET':
        return jsonify({"keys": [svc.key_public(c) for c in svc.keys_for_user(user.id)],
                        "max_active_keys": svc.MAX_KEYS_PER_USER}), 200

    data = request.get_json(silent=True) or {}
    name = str(data.get('name') or '').strip()
    if not name:
        return jsonify({"message": "name is required"}), 400
    if len(name) > 120:
        return jsonify({"message": "name must be 120 characters or fewer"}), 400
    if len(svc.keys_for_user(user.id, active_only=True)) >= svc.MAX_KEYS_PER_USER:
        return jsonify({"message": f"You can have up to {svc.MAX_KEYS_PER_USER} active keys. "
                                   "Revoke one to create another."}), 409
    client, raw = svc.create_api_client(name, plan="free", owner_user_id=user.id)
    body = svc.key_public(client)
    return jsonify({"id": body["id"], "name": body["name"], "key_prefix": body["key_prefix"],
                    "api_key": raw, "plan": body["plan"], "created_at": body["created_at"],
                    "message": "Store this key now; it won't be shown again."}), 201


@passport_bp.route('/keys/<int:key_id>', methods=['DELETE'])
@jwt_required()
@limiter.limit("30 per minute")
def revoke_developer_key(key_id):
    from app.models.passport import PassportApiClient
    user = _signed_in_user()
    if user is None:
        return jsonify({"message": "Sign in with a Siiqo account to manage API keys."}), 403
    client = db.session.get(PassportApiClient, key_id)
    # Someone else's key (or a platform key) looks the same as a missing one.
    if client is None or client.owner_user_id != user.id:
        return jsonify({"message": "Key not found"}), 404
    client.is_active = False
    db.session.commit()
    return jsonify({"id": client.id, "is_active": False, "message": "Key revoked."}), 200
