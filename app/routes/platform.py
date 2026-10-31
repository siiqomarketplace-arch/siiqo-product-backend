"""
Trust Platform console (signed-in pages on siiqo.com), mounted at /api/platform.

- /developers: keys with tier and monthly usage, Growth checkout, webhooks
  (secret shown once) and their delivery log.
- Trust tab: "My websites and handles" (domain / handle ownership claims).
- Superadmin: Scale tier limits per key (audit-logged).
"""
from datetime import datetime, timezone

from flask import Blueprint, jsonify, request
from flask_jwt_extended import get_jwt_identity, jwt_required

from app.extensions import db, limiter
from app.services import ownership
from app.services import passport as passport_svc
from app.services import platform as platform_svc
from app.services import platform_webhooks as webhooks

platform_bp = Blueprint('platform', __name__)


def _user():
    from app.models.user import User
    try:
        user_id = int(get_jwt_identity())
    except (TypeError, ValueError):
        return None
    user = db.session.get(User, user_id)
    return user if user is not None and user.is_active else None


def _own_key(user, key_id):
    from app.models.passport import PassportApiClient
    client = db.session.get(PassportApiClient, key_id)
    if client is None or client.owner_user_id != user.id:
        return None
    return client


def _key_view(client):
    return {**passport_svc.key_public(client), "usage": platform_svc.usage_summary(client)}


_SIGN_IN = {"message": "Sign in with a Siiqo account to manage API keys."}
_NO_KEY = {"message": "Key not found"}


# ── developer keys ─────────────────────────────────────────────────────────

@platform_bp.route('/keys', methods=['GET'])
@jwt_required()
@limiter.limit("60 per minute")
def keys():
    user = _user()
    if user is None:
        return jsonify(_SIGN_IN), 403
    price = platform_svc.growth_price_ngn()
    return jsonify({
        "keys": [_key_view(c) for c in passport_svc.keys_for_user(user.id)],
        "max_active_keys": passport_svc.MAX_KEYS_PER_USER,
        "tiers": {
            "free": {"monthly_limit": platform_svc.TIERS["free"][0], "rate_per_minute": platform_svc.TIERS["free"][1]},
            "growth": {"monthly_limit": platform_svc.TIERS["growth"][0],
                       "rate_per_minute": platform_svc.TIERS["growth"][1], "price_ngn": price,
                       "available": bool(price and platform_svc.growth_plan_code())},
            "test": {"monthly_limit": platform_svc.TEST_LIMITS[0], "rate_per_minute": platform_svc.TEST_LIMITS[1]},
        },
        "test_fixtures": {"slugs": list(platform_svc.FIXTURE_SLUGS),
                          "identifiers": sorted(platform_svc.FIXTURE_IDENTIFIERS)},
    }), 200


@platform_bp.route('/keys/<int:key_id>/growth', methods=['POST'])
@jwt_required()
@limiter.limit("10 per minute")
def growth_checkout(key_id):
    user = _user()
    if user is None:
        return jsonify(_SIGN_IN), 403
    client = _own_key(user, key_id)
    if client is None:
        return jsonify(_NO_KEY), 404
    try:
        result = platform_svc.start_growth_checkout(client, user)
    except platform_svc.GrowthUnavailable as e:
        return jsonify({"message": e.message}), e.status
    return jsonify(result), 200


# ── webhooks ───────────────────────────────────────────────────────────────

def _own_endpoint(client, endpoint_id):
    from app.models.platform import WebhookEndpoint
    endpoint = db.session.get(WebhookEndpoint, endpoint_id)
    if endpoint is None or endpoint.client_id != client.id or endpoint.deleted_at is not None:
        return None
    return endpoint


@platform_bp.route('/keys/<int:key_id>/webhooks', methods=['GET', 'POST'])
@jwt_required()
@limiter.limit("30 per minute")
def key_webhooks(key_id):
    user = _user()
    if user is None:
        return jsonify(_SIGN_IN), 403
    client = _own_key(user, key_id)
    if client is None:
        return jsonify(_NO_KEY), 404
    if request.method == 'GET':
        return jsonify({"webhooks": [webhooks.endpoint_public(e) for e in webhooks.endpoints_for(client)],
                        "events": list(webhooks.EVENTS)}), 200
    if not client.is_active:
        return jsonify({"message": "This key is revoked."}), 400
    data = request.get_json(silent=True) or {}
    try:
        endpoint, secret = webhooks.create_endpoint(client, data.get('url'), data.get('events'))
    except webhooks.WebhookError as e:
        return jsonify({"code": e.code, "message": e.message}), e.status
    return jsonify({"webhook": webhooks.endpoint_public(endpoint), "secret": secret,
                    "message": "Store this secret now; it won't be shown again."}), 201


@platform_bp.route('/keys/<int:key_id>/webhooks/<int:endpoint_id>', methods=['DELETE'])
@jwt_required()
@limiter.limit("30 per minute")
def key_webhook_delete(key_id, endpoint_id):
    user = _user()
    if user is None:
        return jsonify(_SIGN_IN), 403
    client = _own_key(user, key_id)
    endpoint = _own_endpoint(client, endpoint_id) if client else None
    if endpoint is None:
        return jsonify({"message": "Webhook not found"}), 404
    webhooks.delete_endpoint(endpoint)
    return jsonify({"id": endpoint_id, "deleted": True}), 200


@platform_bp.route('/keys/<int:key_id>/webhooks/<int:endpoint_id>/deliveries', methods=['GET'])
@jwt_required()
@limiter.limit("60 per minute")
def key_webhook_deliveries(key_id, endpoint_id):
    user = _user()
    if user is None:
        return jsonify(_SIGN_IN), 403
    client = _own_key(user, key_id)
    endpoint = _own_endpoint(client, endpoint_id) if client else None
    if endpoint is None:
        return jsonify({"message": "Webhook not found"}), 404
    return jsonify({"deliveries": [webhooks.delivery_public(d) for d in webhooks.deliveries_for(endpoint)]}), 200


@platform_bp.route('/keys/<int:key_id>/webhooks/<int:endpoint_id>/test', methods=['POST'])
@jwt_required()
@limiter.limit("10 per minute")
def key_webhook_test(key_id, endpoint_id):
    user = _user()
    if user is None:
        return jsonify(_SIGN_IN), 403
    client = _own_key(user, key_id)
    endpoint = _own_endpoint(client, endpoint_id) if client else None
    if endpoint is None:
        return jsonify({"message": "Webhook not found"}), 404
    if not endpoint.is_active:
        return jsonify({"message": "This webhook is disabled. Delete it and add it again."}), 409
    d = webhooks.send_test_event(endpoint)
    db.session.refresh(d)
    return jsonify({"delivery": webhooks.delivery_public(d)}), 202


# ── seller: my websites and handles ────────────────────────────────────────

def _seller():
    from app.models.user import Storefront
    user = _user()
    if user is None:
        return None
    return user if Storefront.query.filter_by(vendor_id=user.id).first() is not None else None


def _own_claim(user, claim_id):
    from app.models.platform import OwnershipClaim
    claim = db.session.get(OwnershipClaim, claim_id)
    return claim if claim is not None and claim.vendor_id == user.id else None


_NO_STORE = {"message": "Create your store to add your websites and handles."}


@platform_bp.route('/ownership', methods=['GET', 'POST'])
@jwt_required()
@limiter.limit("30 per minute")
def ownership_claims():
    from app.models.platform import OwnershipClaim
    user = _seller()
    if user is None:
        return jsonify(_NO_STORE), 403
    if request.method == 'GET':
        claims = OwnershipClaim.query.filter_by(vendor_id=user.id).order_by(OwnershipClaim.id.asc()).all()
        return jsonify({"claims": [ownership.claim_public(c) for c in claims],
                        "networks": list(ownership.NETWORKS)}), 200
    data = request.get_json(silent=True) or {}
    try:
        claim, created = ownership.create_claim(user.id, str(data.get('kind') or ''), str(data.get('value') or ''),
                                                data.get('network'))
    except ownership.OwnershipError as e:
        return jsonify({"code": e.code, "message": e.message}), e.status
    return jsonify({"claim": ownership.claim_public(claim)}), 201 if created else 200


@platform_bp.route('/ownership/<int:claim_id>/check', methods=['POST'])
@jwt_required()
@limiter.limit("10 per minute")
def ownership_check_now(claim_id):
    user = _seller()
    if user is None:
        return jsonify(_NO_STORE), 403
    claim = _own_claim(user, claim_id)
    if claim is None:
        return jsonify({"message": "Not found"}), 404
    ownership.request_check(claim)
    db.session.refresh(claim)
    return jsonify({"claim": ownership.claim_public(claim)}), 202


@platform_bp.route('/ownership/<int:claim_id>', methods=['DELETE'])
@jwt_required()
@limiter.limit("30 per minute")
def ownership_delete(claim_id):
    user = _seller()
    if user is None:
        return jsonify(_NO_STORE), 403
    claim = _own_claim(user, claim_id)
    if claim is None:
        return jsonify({"message": "Not found"}), 404
    ownership.remove_claim(claim)
    return jsonify({"id": claim_id, "deleted": True}), 200


# ── superadmin: Scale tier ─────────────────────────────────────────────────

def _superadmin():
    from app.routes.admin import _parse_admin_id, _require_superadmin
    try:
        return _require_superadmin(_parse_admin_id(get_jwt_identity()))
    except (TypeError, ValueError):
        return None


@platform_bp.route('/admin/clients/<int:client_id>/tier', methods=['GET', 'PUT'])
@jwt_required()
def admin_client_tier(client_id):
    """PUT {"tier": "scale", "monthly_limit": int, "rate_per_minute": int, "note"?}
    or {"tier": "default"} to remove the Scale limits."""
    from app.models.audit import AdminAuditLog
    from app.models.passport import PassportApiClient
    admin = _superadmin()
    if not admin:
        return jsonify({"message": "Superadmin access required"}), 403
    client = db.session.get(PassportApiClient, client_id)
    if client is None:
        return jsonify({"message": "Key not found"}), 404
    if request.method == 'GET':
        return jsonify({"key": _key_view(client)}), 200
    if platform_svc.is_test(client):
        return jsonify({"message": "Test keys keep the test quota."}), 400

    data = request.get_json(silent=True) or {}
    tier = str(data.get('tier') or '').strip().lower()
    if tier == "scale":
        try:
            monthly = int(data.get('monthly_limit'))
            rate = int(data.get('rate_per_minute'))
        except (TypeError, ValueError):
            return jsonify({"message": "monthly_limit and rate_per_minute must be whole numbers"}), 400
        if not (1 <= monthly <= 1_000_000_000) or not (1 <= rate <= 100_000):
            return jsonify({"message": "monthly_limit or rate_per_minute is out of range"}), 400
        _row, before = platform_svc.set_scale_limits(client, monthly, rate, data.get('note'), admin.id)
        after = {"tier": "scale", "monthly_limit": monthly, "rate_per_minute": rate}
        action = "PLATFORM_TIER_SET"
    elif tier == "default":
        before = platform_svc.clear_scale_limits(client)
        after = {"tier": "default"}
        action = "PLATFORM_TIER_CLEARED"
    else:
        return jsonify({"message": "tier must be 'scale' or 'default'"}), 400

    db.session.add(AdminAuditLog(
        admin_id=admin.id, admin_email=admin.email, admin_role=admin.role,
        action=action, resource_type="PassportApiClient", resource_id=str(client.id),
        details={"before": before, "after": after, "note": (data.get('note') or None)},
        ip_address=(request.remote_addr or "unknown")[:45],
        user_agent=request.headers.get("User-Agent"),
        timestamp=datetime.now(timezone.utc)))
    db.session.commit()
    return jsonify({"key": _key_view(client)}), 200
