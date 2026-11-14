"""Marketing system routes (docs/MARKETING_SYSTEM_PLAN.md).

    GET  /api/public/r/<code>              resolve a short code, count the scan (no auth)
    POST /api/agents/apply                 apply to be an agent (user JWT)
    GET  /api/agents/me                    my code, today/week/totals, earnings
    GET  /api/agents/me/sellers            the sellers credited to me
    GET  /api/agents/me/earnings           my earnings ledger
    GET  /api/agents/leaderboard           week or month, top 10 + teams
    GET  /api/agents/prefill               what we know for the form (bank numbers masked)
    GET  /api/agents/teams?state=          active teams, for the form
    GET  /api/agents/team                  a team lead's read-only team view
    GET/POST /api/agents/riders            a logistics company's rider codes
    PATCH /api/agents/riders/<code_id>     rename or pause a rider code
    GET  /api/vendor/growth-kit            the seller's sticker and invite codes and results

Admin routes are in app/routes/admin_marketing.py. Logic is in
app/services/marketing.py.
"""
from flask import Blueprint, jsonify, request
from flask_jwt_extended import get_jwt_identity, jwt_required

from app.extensions import db, limiter
from app.models.marketing import AgentEarning, AgentProfile, AgentTeam, MarketingCode, SellerAcquisition
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
        return None  # admin tokens are not user tokens (partner logins issue normal user tokens)


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


# ── becoming an agent: pre-fill and team lists ───────────────────────────────

@agents_bp.route("/prefill", methods=["GET"])
@jwt_required()
def agent_prefill():
    user = _current_user()
    if user is None:
        return _not_signed_in()
    return jsonify(svc.prefill(user)), 200


@agents_bp.route("/teams", methods=["GET"])
@jwt_required()
def agent_teams():
    if _current_user() is None:
        return _not_signed_in()
    q = AgentTeam.query.filter(AgentTeam.active.is_(True))
    state = svc.canonical_state(request.args.get("state"))
    if request.args.get("state") and state is None:
        return jsonify({"teams": []}), 200
    if state:
        q = q.filter(AgentTeam.state == state)
    rows = q.order_by(AgentTeam.state, AgentTeam.city, AgentTeam.name).limit(500).all()
    return jsonify({"teams": [svc.team_dict(t) for t in rows]}), 200


# ── team lead: read-only view of their team ──────────────────────────────────

@agents_bp.route("/team", methods=["GET"])
@jwt_required()
def agent_team():
    user = _current_user()
    if user is None:
        return _not_signed_in()
    teams = svc.led_teams(user.id)
    if not teams:
        return jsonify({"code": "NOT_A_TEAM_LEAD", "message": "Only a team lead can see a team."}), 403
    team = teams[0]
    wanted = request.args.get("team_id")
    if wanted:
        team = next((t for t in teams if str(t.id) == str(wanted)), None)
        if team is None:
            return jsonify({"code": "NOT_A_TEAM_LEAD", "message": "You don't lead that team."}), 403
    return jsonify({**svc.team_view(team), "teams": [svc.team_dict(t) for t in teams]}), 200


# ── logistics companies: rider codes ─────────────────────────────────────────

def _company():
    """(user, profile, error_response) for an APPROVED logistics company."""
    user, profile = _my_profile()
    if user is None:
        return None, None, _not_signed_in()
    if profile is None or profile.agent_type != "logistics_company" or profile.status != "APPROVED":
        return user, profile, (jsonify({"code": "NOT_A_COMPANY",
                                        "message": "Only an approved logistics company can add riders."}), 403)
    return user, profile, None


@agents_bp.route("/riders", methods=["GET"])
@jwt_required()
def rider_list():
    _, profile, error = _company()
    if error:
        return error
    rows = svc.rider_codes_query(profile).order_by(MarketingCode.created_at.desc(), MarketingCode.id.desc()).all()
    return jsonify({"riders": [svc.rider_row(profile, c) for c in rows], "total": len(rows),
                    "limit": svc.MAX_RIDER_CODES}), 200


@agents_bp.route("/riders", methods=["POST"])
@jwt_required()
@limiter.limit("120 per hour")
def rider_create():
    _, profile, error = _company()
    if error:
        return error
    try:
        code = svc.create_rider_code(profile, request.get_json(silent=True) or {})
    except svc.MarketingError as e:
        db.session.rollback()
        return _err(e)
    return jsonify({"rider": svc.rider_row(profile, code)}), 201


@agents_bp.route("/riders/<int:code_id>", methods=["PATCH"])
@jwt_required()
@limiter.limit("240 per hour")
def rider_update(code_id):
    _, profile, error = _company()
    if error:
        return error
    code = svc.rider_codes_query(profile).filter(MarketingCode.id == code_id).first()
    if code is None:
        return jsonify({"code": "NOT_FOUND", "message": "Rider code not found."}), 404
    try:
        svc.update_rider_code(code, request.get_json(silent=True) or {})
    except svc.MarketingError as e:
        db.session.rollback()
        return _err(e)
    return jsonify({"rider": svc.rider_row(profile, code)}), 200


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
