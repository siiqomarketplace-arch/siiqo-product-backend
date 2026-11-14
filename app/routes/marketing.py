"""Marketing system routes (docs/MARKETING_SYSTEM_PLAN.md).

    GET  /api/public/r/<code>              resolve a short code, count the scan (no auth)
    POST /api/agents/apply                 apply to be an agent (user JWT)
    GET  /api/agents/me                    my code, today/week/totals, earnings
    GET  /api/agents/me/sellers            the sellers credited to me
    GET  /api/agents/me/earnings           my earnings ledger
    GET  /api/agents/leaderboard           week or month, top 10 + teams
    GET  /api/vendor/growth-kit            the seller's sticker and invite codes and results

Admin routes are in app/routes/admin_marketing.py. Logic is in
app/services/marketing.py.
"""
from flask import Blueprint, jsonify, request
from flask_jwt_extended import get_jwt_identity, jwt_required

from app.extensions import db, limiter
from app.models.marketing import AgentEarning, AgentProfile, SellerAcquisition
from app.models.user import User
from app.services import marketing as svc

marketing_public_bp = Blueprint("marketing_public", __name__)
agents_bp = Blueprint("agents", __name__)
growth_kit_bp = Blueprint("growth_kit", __name__)


def _err(e: svc.MarketingError):
    return jsonify(e.to_dict()), e.status


def _current_user():
    identity = get_jwt_identity()
    try:
        return db.session.get(User, int(identity))
    except (TypeError, ValueError):
        return None  # admin and partner tokens are not user tokens


def _not_signed_in():
    return jsonify({"code": "UNAUTHORIZED", "message": "Please log in."}), 401


# ── public ───────────────────────────────────────────────────────────────────

@marketing_public_bp.route("/r/<code>", methods=["GET"])
@limiter.limit("120 per minute")
def resolve_code(code):
    status, body = svc.resolve_code(code, request.args.get("sid"))
    return jsonify(body), status


# ── agents ───────────────────────────────────────────────────────────────────

def _my_profile():
    user = _current_user()
    if user is None:
        return None, None
    return user, AgentProfile.query.filter_by(user_id=user.id).first()


def _not_an_agent():
    return jsonify({"code": "NOT_AN_AGENT", "message": "You are not a Siiqo agent yet."}), 404


@agents_bp.route("/apply", methods=["POST"])
@jwt_required()
@limiter.limit("10 per hour")
def agent_apply():
    user = _current_user()
    if user is None:
        return _not_signed_in()
    try:
        profile = svc.apply(user, request.get_json(silent=True) or {})
    except svc.MarketingError as e:
        return _err(e)
    return jsonify({"status": profile.status}), 201


@agents_bp.route("/me", methods=["GET"])
@jwt_required()
def agent_me():
    user, profile = _my_profile()
    if user is None:
        return _not_signed_in()
    if profile is None:
        return _not_an_agent()
    return jsonify(svc.agent_me(profile)), 200


@agents_bp.route("/me/sellers", methods=["GET"])
@jwt_required()
def agent_sellers():
    user, profile = _my_profile()
    if user is None:
        return _not_signed_in()
    if profile is None:
        return _not_an_agent()
    page, per_page = svc.page(request.args)
    q = (SellerAcquisition.query.filter(SellerAcquisition.agent_user_id == user.id)
         .order_by(SellerAcquisition.signed_up_at.desc(), SellerAcquisition.id.desc()))
    total = q.count()
    rows = q.offset((page - 1) * per_page).limit(per_page).all()
    return jsonify({"sellers": [svc.seller_row(a) for a in rows], "total": total, "page": page,
                    "pages": svc.pages(total, per_page)}), 200


@agents_bp.route("/me/earnings", methods=["GET"])
@jwt_required()
def agent_earnings():
    user, profile = _my_profile()
    if user is None:
        return _not_signed_in()
    if profile is None:
        return _not_an_agent()
    page, per_page = svc.page(request.args)
    q = AgentEarning.query.filter(AgentEarning.agent_user_id == user.id)
    totals = svc.earning_totals(q)
    total = q.count()
    rows = q.order_by(AgentEarning.earned_at.desc(), AgentEarning.id.desc()) \
        .offset((page - 1) * per_page).limit(per_page).all()
    earnings = []
    for e in rows:
        row = svc.earning_row(e)
        earnings.append({k: row[k] for k in ("id", "kind", "sale_number", "order_id", "amount", "status",
                                             "store_name", "earned_at", "payable_at", "paid_at", "reference")})
    return jsonify({"earnings": earnings, "totals": totals, "total": total, "page": page,
                    "pages": svc.pages(total, per_page)}), 200


@agents_bp.route("/leaderboard", methods=["GET"])
@jwt_required()
def agent_leaderboard():
    user, profile = _my_profile()
    if user is None:
        return _not_signed_in()
    if profile is None:
        return _not_an_agent()
    period = (request.args.get("period") or "week").strip().lower()
    if period not in ("week", "month"):
        period = "week"
    return jsonify(svc.leaderboard(user.id, period)), 200


# ── seller growth kit ────────────────────────────────────────────────────────

@growth_kit_bp.route("/growth-kit", methods=["GET"])
@jwt_required()
def growth_kit():
    user = _current_user()
    if user is None:
        return _not_signed_in()
    storefront = user.storefront
    if storefront is None:
        return jsonify({"code": "NO_STORE", "message": "Set up your store first."}), 404
    try:
        return jsonify(svc.growth_kit(user, storefront)), 200
    except svc.MarketingError as e:
        db.session.rollback()
        return _err(e)
