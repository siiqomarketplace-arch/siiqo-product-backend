"""Admin side of the marketing system. Registered on the admin blueprint
(imported at the bottom of app/routes/admin.py). Any admin may read and run
campaigns, codes and agents; marking earnings paid and reversing them needs
a superadmin. Every write is audit-logged.

    GET/POST   /api/admin/marketing/campaigns           PATCH /api/admin/marketing/campaigns/<id>
    GET/POST   /api/admin/marketing/codes               PATCH /api/admin/marketing/codes/<id>
    GET        /api/admin/marketing/board?from&to
    GET        /api/admin/agents                        GET   /api/admin/agents/<id>
    POST       /api/admin/agents/<id>/approve|reject|pause|resume
    GET        /api/admin/marketing/earnings
    POST       /api/admin/marketing/earnings/mark-paid  (superadmin)
    POST       /api/admin/marketing/earnings/<id>/reverse (superadmin)
    GET        /api/admin/marketing/flags
"""
import logging
from datetime import datetime, timezone

from flask import jsonify, request
from flask_jwt_extended import get_jwt_identity, jwt_required
from sqlalchemy import String, cast, func, or_
from sqlalchemy.exc import IntegrityError

from app.extensions import db, limiter
from app.models.audit import AdminAuditLog
from app.models.marketing import (AgentEarning, AgentProfile, MarketingCampaign, MarketingCode,
                                  SellerAcquisition)
from app.models.user import Storefront, User
from app.routes.admin import _client_ip, _get_admin, _parse_admin_id, _require_superadmin, admin_bp
from app.services import marketing as svc


def _admin():
    return _get_admin(_parse_admin_id(get_jwt_identity()))


def _superadmin():
    return _require_superadmin(_parse_admin_id(get_jwt_identity()))


def _forbidden(message="Unauthorized"):
    return jsonify({"code": "FORBIDDEN", "message": message}), 403


def _err(e: svc.MarketingError):
    db.session.rollback()
    return jsonify(e.to_dict()), e.status


def _audit(admin, action, resource_type, resource_id, details=None):
    db.session.add(AdminAuditLog(
        admin_id=admin.id, admin_email=admin.email, admin_role=admin.role, action=action,
        resource_type=resource_type, resource_id=str(resource_id), details=details or {},
        ip_address=(_client_ip() or "unknown")[:45],
        user_agent=(request.headers.get("User-Agent") or "")[:500],
    ))


def _body():
    data = request.get_json(silent=True)
    return data if isinstance(data, dict) else {}


def _not_found(what):
    return jsonify({"code": "NOT_FOUND", "message": f"{what} not found."}), 404


# ── campaigns ────────────────────────────────────────────────────────────────

def _campaign_fields(data, campaign=None):
    """Validated changes for a campaign (create when campaign is None)."""
    changes = {}
    if "name" in data or campaign is None:
        name = data.get("name").strip() if isinstance(data.get("name"), str) else ""
        if not name or len(name) > 120:
            raise svc.MarketingError(400, "VALIDATION_ERROR", "Give the campaign a name (up to 120 letters).",
                                     field="name")
        changes["name"] = name
    if "channel" in data or campaign is None:
        channel = (data.get("channel") or "").strip().lower() if isinstance(data.get("channel"), str) else ""
        if channel not in MarketingCampaign.CHANNELS:
            raise svc.MarketingError(400, "VALIDATION_ERROR", "Pick a channel.", field="channel")
        changes["channel"] = channel
    if "location" in data:
        loc = data.get("location")
        changes["location"] = (loc.strip()[:120] or None) if isinstance(loc, str) else None
    if "notes" in data:
        notes = data.get("notes")
        changes["notes"] = (notes.strip()[:5000] or None) if isinstance(notes, str) else None
    if "budget_ngn" in data:
        changes["budget_ngn"] = svc.parse_amount(data.get("budget_ngn"), "budget_ngn")
    if "spend_ngn" in data and campaign is not None:
        changes["spend_ngn"] = svc.parse_amount(data.get("spend_ngn"), "spend_ngn")
    if "status" in data and campaign is not None:
        status = (data.get("status") or "").strip().upper() if isinstance(data.get("status"), str) else ""
        if status not in MarketingCampaign.STATUSES:
            raise svc.MarketingError(400, "VALIDATION_ERROR", "Status is ACTIVE, PAUSED or ENDED.", field="status")
        changes["status"] = status
    return changes


@admin_bp.route("/marketing/campaigns", methods=["GET"])
@jwt_required()
def admin_list_campaigns():
    if not _admin():
        return _forbidden()
    page, per_page = svc.page(request.args, default_per_page=50)
    q = MarketingCampaign.query
    status = (request.args.get("status") or "").strip().upper()
    if status in MarketingCampaign.STATUSES:
        q = q.filter(MarketingCampaign.status == status)
    total = q.count()
    rows = q.order_by(MarketingCampaign.created_at.desc(), MarketingCampaign.id.desc()) \
        .offset((page - 1) * per_page).limit(per_page).all()
    return jsonify({"campaigns": [svc.campaign_dict(c) for c in rows], "total": total}), 200


@admin_bp.route("/marketing/campaigns", methods=["POST"])
@jwt_required()
@limiter.limit("60 per hour")
def admin_create_campaign():
    admin = _admin()
    if not admin:
        return _forbidden()
    try:
        changes = _campaign_fields(_body())
        campaign = MarketingCampaign(slug=svc.unique_campaign_slug(changes["name"]), status="ACTIVE",
                                     created_by_admin_id=admin.id, spend_ngn=0,
                                     created_at=datetime.now(timezone.utc), **changes)
        db.session.add(campaign)
        db.session.flush()
        _audit(admin, "MARKETING_CAMPAIGN_CREATE", "MarketingCampaign", campaign.id,
               {"name": campaign.name, "channel": campaign.channel})
        db.session.commit()
    except svc.MarketingError as e:
        return _err(e)
    return jsonify({"campaign": svc.campaign_dict(campaign)}), 201


@admin_bp.route("/marketing/campaigns/<int:campaign_id>", methods=["PATCH"])
@jwt_required()
@limiter.limit("120 per hour")
def admin_update_campaign(campaign_id):
    admin = _admin()
    if not admin:
        return _forbidden()
    campaign = db.session.get(MarketingCampaign, campaign_id)
    if campaign is None:
        return _not_found("Campaign")
    try:
        changes = _campaign_fields(_body(), campaign)
    except svc.MarketingError as e:
        return _err(e)
    before = {k: str(getattr(campaign, k)) for k in changes}
    for k, v in changes.items():
        setattr(campaign, k, v)
    _audit(admin, "MARKETING_CAMPAIGN_UPDATE", "MarketingCampaign", campaign.id,
           {"before": before, "after": {k: str(v) for k, v in changes.items()}})
    db.session.commit()
    return jsonify({"campaign": svc.campaign_dict(campaign)}), 200


# ── codes ────────────────────────────────────────────────────────────────────

def _campaign_id(value):
    if value in (None, ""):
        return None
    try:
        cid = int(value)
    except (TypeError, ValueError):
        raise svc.MarketingError(400, "CAMPAIGN_NOT_FOUND", "That campaign does not exist.", field="campaign_id")
    if db.session.get(MarketingCampaign, cid) is None:
        raise svc.MarketingError(400, "CAMPAIGN_NOT_FOUND", "That campaign does not exist.", field="campaign_id")
    return cid


@admin_bp.route("/marketing/codes", methods=["GET"])
@jwt_required()
def admin_list_codes():
    if not _admin():
        return _forbidden()
    page, per_page = svc.page(request.args, default_per_page=50)
    q = MarketingCode.query
    if request.args.get("campaign_id"):
        try:
            q = q.filter(MarketingCode.campaign_id == int(request.args["campaign_id"]))
        except ValueError:
            pass
    kind = (request.args.get("kind") or "").strip().lower()
    if kind in MarketingCode.KINDS:
        q = q.filter(MarketingCode.kind == kind)
    term = (request.args.get("q") or "").strip()
    if term:
        like = f"%{term.lower()}%"
        q = q.filter(or_(func.lower(MarketingCode.code).like(like), func.lower(MarketingCode.label).like(like)))
    total = q.count()
    rows = q.order_by(MarketingCode.created_at.desc(), MarketingCode.id.desc()) \
        .offset((page - 1) * per_page).limit(per_page).all()
    return jsonify({"codes": [svc.code_dict(c) for c in rows], "total": total}), 200


@admin_bp.route("/marketing/codes", methods=["POST"])
@jwt_required()
@limiter.limit("120 per hour")
def admin_create_code():
    admin = _admin()
    if not admin:
        return _forbidden()
    data = _body()
    kind = (data.get("kind") or "").strip().lower() if isinstance(data.get("kind"), str) else ""
    try:
        if kind not in svc.ADMIN_CODE_KINDS:
            raise svc.MarketingError(400, "INVALID_KIND", "Pick a valid code type.", field="kind")
        code = svc.create_code(kind, data.get("destination"), label=data.get("label") if isinstance(
            data.get("label"), str) else None, campaign_id=_campaign_id(data.get("campaign_id")),
            code=data.get("code") if isinstance(data.get("code"), str) else None)
        db.session.flush()
        _audit(admin, "MARKETING_CODE_CREATE", "MarketingCode", code.id,
               {"code": code.code, "kind": code.kind, "destination": code.destination})
        db.session.commit()
    except svc.MarketingError as e:
        return _err(e)
    except IntegrityError:
        db.session.rollback()
        return jsonify({"code": "CODE_TAKEN", "message": "That code is already in use.", "field": "code"}), 409
    except Exception:
        db.session.rollback()
        logging.exception("[MARKETING] code create failed")
        return jsonify({"code": "CODE_CREATE_FAILED",
                        "message": "We couldn't create this code. Nothing was saved. Try again."}), 500
    return jsonify({"code": svc.code_dict(code)}), 201


@admin_bp.route("/marketing/codes/<int:code_id>", methods=["PATCH"])
@jwt_required()
@limiter.limit("240 per hour")
def admin_update_code(code_id):
    admin = _admin()
    if not admin:
        return _forbidden()
    code = db.session.get(MarketingCode, code_id)
    if code is None:
        return _not_found("Code")
    data = _body()
    changes = {}
    try:
        if "label" in data:
            label = data.get("label")
            changes["label"] = (label.strip()[:120] or None) if isinstance(label, str) else None
        if "destination" in data:
            changes["destination"] = svc.validate_destination(data.get("destination"))
        if "active" in data:
            if not isinstance(data.get("active"), bool):
                raise svc.MarketingError(400, "VALIDATION_ERROR", "active is true or false.", field="active")
            changes["active"] = data["active"]
        if "campaign_id" in data:
            changes["campaign_id"] = _campaign_id(data.get("campaign_id"))
    except svc.MarketingError as e:
        return _err(e)
    before = {k: getattr(code, k) for k in changes}
    for k, v in changes.items():
        setattr(code, k, v)
    _audit(admin, "MARKETING_CODE_UPDATE", "MarketingCode", code.id,
           {"code": code.code, "before": before, "after": changes})
    db.session.commit()
    return jsonify({"code": svc.code_dict(code)}), 200


# ── board ────────────────────────────────────────────────────────────────────

@admin_bp.route("/marketing/board", methods=["GET"])
@jwt_required()
def admin_marketing_board():
    if not _admin():
        return _forbidden()
    try:
        return jsonify(svc.board(request.args.get("from"), request.args.get("to"))), 200
    except svc.MarketingError as e:
        return _err(e)


# ── agents ───────────────────────────────────────────────────────────────────

def _agent_summary(p):
    acqs = SellerAcquisition.query.filter_by(agent_user_id=p.user_id)
    totals = svc.earning_totals(AgentEarning.query.filter(AgentEarning.agent_user_id == p.user_id))
    flags_count = sum(len(a.flags or []) for a in acqs.all())
    return {
        "id": p.id, "user_id": p.user_id, "full_name": p.full_name, "phone": p.phone, "city": p.city,
        "agent_type": p.agent_type, "team": p.team, "status": p.status,
        "code": p.code.code if p.code_id and p.code else None,
        "signups": acqs.count(),
        "setups": acqs.filter(SellerAcquisition.setup_at.isnot(None)).count(),
        "first_sales": acqs.filter(SellerAcquisition.first_paid_order_at.isnot(None)).count(),
        "rewarded_sales": svc.live_sale_earnings().filter(AgentEarning.agent_user_id == p.user_id).count(),
        **totals, "flags_count": flags_count, "created_at": svc._iso(p.created_at),
    }


@admin_bp.route("/agents", methods=["GET"])
@jwt_required()
def admin_list_agents():
    if not _admin():
        return _forbidden()
    page, per_page = svc.page(request.args, default_per_page=50)
    q = AgentProfile.query
    status = (request.args.get("status") or "").strip().upper()
    if status in AgentProfile.STATUSES:
        q = q.filter(AgentProfile.status == status)
    term = (request.args.get("q") or "").strip().lower()
    if term:
        like = f"%{term}%"
        q = q.filter(or_(func.lower(AgentProfile.full_name).like(like), AgentProfile.phone.like(like),
                         func.lower(AgentProfile.city).like(like), func.lower(AgentProfile.team).like(like)))
    total = q.count()
    rows = q.order_by(AgentProfile.created_at.desc(), AgentProfile.id.desc()) \
        .offset((page - 1) * per_page).limit(per_page).all()
    return jsonify({"agents": [_agent_summary(p) for p in rows], "total": total}), 200


@admin_bp.route("/agents/<int:agent_id>", methods=["GET"])
@jwt_required()
def admin_get_agent(agent_id):
    if not _admin():
        return _forbidden()
    p = db.session.get(AgentProfile, agent_id)
    if p is None:
        return _not_found("Agent")
    acqs = (SellerAcquisition.query.filter_by(agent_user_id=p.user_id)
            .order_by(SellerAcquisition.signed_up_at.desc()).limit(200).all())
    earnings = (AgentEarning.query.filter_by(agent_user_id=p.user_id)
                .order_by(AgentEarning.earned_at.desc()).limit(200).all())
    user = db.session.get(User, p.user_id)
    agent = {
        **_agent_summary(p),
        "email": user.email if user else None,
        "bank_name": p.bank_name, "bank_code": p.bank_code, "account_number": p.account_number,
        "account_name": p.account_name, "agreed_conduct_at": svc._iso(p.agreed_conduct_at),
        "approved_at": svc._iso(p.approved_at), "approved_by_admin_id": p.approved_by_admin_id,
        "link": svc.code_link(p.code.code) if p.code_id and p.code else None,
    }
    sellers = [svc.seller_row(a) for a in acqs]
    flags = [{"acquisition_id": a.id, "seller_id": a.seller_user_id, "flags": list(a.flags or [])}
             for a in acqs if a.flags]
    return jsonify({"agent": agent, "sellers": sellers,
                    "earnings": [svc.earning_row(e) for e in earnings], "flags": flags}), 200


_AGENT_MESSAGES = {
    "approve": ("You are now a Siiqo agent",
                "Your agent application was approved. Open your agent page to see your code and link."),
    "reject": ("Your agent application", "Your agent application was not approved this time."),
    "pause": ("Your agent account is paused",
              "Your agent account is paused. New sellers will not earn you rewards until it is resumed."),
    "resume": ("Your agent account is active again", "Your agent account is active again. Keep going."),
}


@admin_bp.route("/agents/<int:agent_id>/<action>", methods=["POST"])
@jwt_required()
@limiter.limit("120 per hour")
def admin_agent_action(agent_id, action):
    if action not in _AGENT_MESSAGES:
        return _not_found("Action")
    admin = _admin()
    if not admin:
        return _forbidden()
    p = db.session.get(AgentProfile, agent_id)
    if p is None:
        return _not_found("Agent")
    data = _body()
    before = p.status
    allowed = {"approve": ("PENDING", "REJECTED", "PAUSED", "APPROVED"), "reject": ("PENDING", "PAUSED", "APPROVED"),
               "pause": ("APPROVED",), "resume": ("PAUSED",)}[action]
    if p.status not in allowed:
        return jsonify({"code": "INVALID_STATUS", "message": f"This agent is {p.status.lower()}.",
                        "status": p.status}), 409
    title, message = _AGENT_MESSAGES[action]
    details = {"from": before}
    try:
        if action == "approve":
            code = svc.approve_agent(p, admin.id)
            details["code"] = code.code
            message += f" Your code is {code.code}."
        elif action == "reject":
            p.status = "REJECTED"
            reason = data.get("reason") if isinstance(data.get("reason"), str) else ""
            reason = reason.strip()[:500]
            if reason:
                details["reason"] = reason
                message += f" Reason: {reason}"
            if p.code_id and p.code:
                p.code.active = False
        elif action == "pause":
            p.status = "PAUSED"
        else:
            p.status = "APPROVED"
        details["to"] = p.status
        svc.notify(p.user_id, title, message)
        _audit(admin, f"AGENT_{action.upper()}", "AgentProfile", p.id, details)
        db.session.commit()
    except svc.MarketingError as e:
        return _err(e)
    return jsonify({"agent": _agent_summary(p)}), 200


# ── earnings ─────────────────────────────────────────────────────────────────

@admin_bp.route("/marketing/earnings", methods=["GET"])
@jwt_required()
def admin_list_earnings():
    if not _admin():
        return _forbidden()
    page, per_page = svc.page(request.args, default_per_page=50)
    q = AgentEarning.query
    status = (request.args.get("status") or "").strip().upper()
    if status in AgentEarning.STATUSES:
        q = q.filter(AgentEarning.status == status)
    if request.args.get("agent_user_id"):
        try:
            q = q.filter(AgentEarning.agent_user_id == int(request.args["agent_user_id"]))
        except ValueError:
            pass
    total = q.count()
    totals = svc.earning_totals(q)
    rows = q.order_by(AgentEarning.earned_at.desc(), AgentEarning.id.desc()) \
        .offset((page - 1) * per_page).limit(per_page).all()
    return jsonify({"earnings": [svc.earning_row(e, with_agent=True) for e in rows], "totals": totals,
                    "total": total}), 200


@admin_bp.route("/marketing/earnings/mark-paid", methods=["POST"])
@jwt_required()
@limiter.limit("60 per hour")
def admin_mark_earnings_paid():
    admin = _superadmin()
    if not admin:
        return _forbidden("Superadmin access required")
    data = _body()
    ids = data.get("ids")
    reference = data.get("reference").strip() if isinstance(data.get("reference"), str) else ""
    if not isinstance(ids, list) or not ids or not all(isinstance(i, int) for i in ids) or len(ids) > 500:
        return jsonify({"code": "VALIDATION_ERROR", "message": "Pick the earnings to mark as paid.",
                        "field": "ids"}), 400
    if not reference or len(reference) > 120:
        return jsonify({"code": "VALIDATION_ERROR", "message": "Enter the bank transfer reference.",
                        "field": "reference"}), 400
    rows = AgentEarning.query.filter(AgentEarning.id.in_(ids), AgentEarning.status == "PAYABLE").all()
    now = datetime.now(timezone.utc)
    amount = 0.0
    for e in rows:
        e.status = "PAID"
        e.paid_at = now
        e.paid_reference = reference
        amount += float(e.amount_ngn or 0)
    _audit(admin, "AGENT_EARNINGS_MARK_PAID", "AgentEarning", ",".join(str(e.id) for e in rows)[:100] or "-",
           {"ids": [e.id for e in rows], "skipped": sorted(set(ids) - {e.id for e in rows}),
            "reference": reference, "amount": amount})
    db.session.commit()
    return jsonify({"paid": len(rows), "amount": amount}), 200


@admin_bp.route("/marketing/earnings/<int:earning_id>/reverse", methods=["POST"])
@jwt_required()
@limiter.limit("60 per hour")
def admin_reverse_earning(earning_id):
    admin = _superadmin()
    if not admin:
        return _forbidden("Superadmin access required")
    e = db.session.get(AgentEarning, earning_id)
    if e is None:
        return _not_found("Earning")
    reason = _body().get("reason")
    reason = reason.strip() if isinstance(reason, str) else ""
    if not reason:
        return jsonify({"code": "VALIDATION_ERROR", "message": "Say why this is being reversed.",
                        "field": "reason"}), 400
    if e.status not in ("HELD", "PAYABLE"):
        return jsonify({"code": "INVALID_STATUS", "message": f"This earning is already {e.status.lower()}.",
                        "status": e.status}), 409
    before = e.status
    e.status = "REVERSED"
    e.reversed_reason = reason[:255]
    _audit(admin, "AGENT_EARNING_REVERSE", "AgentEarning", e.id, {"from": before, "reason": reason[:255]})
    db.session.commit()
    return jsonify({"earning": svc.earning_row(e, with_agent=True)}), 200


# ── flags ────────────────────────────────────────────────────────────────────

@admin_bp.route("/marketing/flags", methods=["GET"])
@jwt_required()
def admin_marketing_flags():
    if not _admin():
        return _forbidden()
    page, per_page = svc.page(request.args, default_per_page=50)
    flags_text = func.coalesce(cast(SellerAcquisition.flags, String), "[]")
    q = SellerAcquisition.query.filter(flags_text.notin_(["[]", "null", ""]))
    total = q.count()
    rows = q.order_by(SellerAcquisition.created_at.desc(), SellerAcquisition.id.desc()) \
        .offset((page - 1) * per_page).limit(per_page).all()
    out = []
    for a in rows:
        sf = Storefront.query.filter_by(vendor_id=a.seller_user_id).first()
        agent = None
        if a.agent_user_id:
            profile = AgentProfile.query.filter_by(user_id=a.agent_user_id).first()
            agent = {"user_id": a.agent_user_id, "name": profile.full_name if profile else None}
        out.append({"acquisition_id": a.id, "seller": {"id": a.seller_user_id,
                                                       "store_name": sf.store_name if sf else None},
                    "agent": agent, "flags": list(a.flags or []), "created_at": svc._iso(a.created_at)})
    return jsonify({"flags": out, "total": total}), 200
