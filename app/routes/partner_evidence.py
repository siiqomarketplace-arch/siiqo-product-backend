"""
Evidence API: the seller's consent list and superadmin controls
(app/services/partner_evidence.py; the partner-facing POST is in v1.py).

Seller (JWT, the store owner):
  GET  /api/vendor/trust-partners                      "Platforms that report to your Passport"
  POST /api/vendor/trust-partners/<client_id>/accept   start counting this platform's reports
  POST /api/vendor/trust-partners/<client_id>/revoke   stop counting; {"disputed": true} also
                                                       tells Siiqo the reports were wrong
Superadmin (audit-logged):
  GET  /api/admin/evidence-partners                    every reporting platform with its signals
  POST /api/admin/evidence-partners/<client_id>/throttle  {"factor": 0.25, "reason": "..."}
  POST /api/admin/evidence-partners/<client_id>/revoke    {"reason": "..."}
  POST /api/admin/evidence-partners/<client_id>/restore   {"reason": "..."}
"""
from flask import Blueprint, jsonify, request
from flask_jwt_extended import get_jwt_identity, jwt_required

from app.extensions import db
from app.services import partner_evidence as evidence
from app.services import trust_v1

trust_partners_bp = Blueprint("trust_partners", __name__)
evidence_admin_bp = Blueprint("evidence_admin", __name__)


def _seller():
    from app.models.user import Storefront, User
    identity = str(get_jwt_identity() or "")
    if not identity.isdigit():
        return None
    user = db.session.get(User, int(identity))
    if user is None or Storefront.query.filter_by(vendor_id=user.id).first() is None:
        return None
    return user


def _error(e):
    res = jsonify(e.body())
    res.status_code = e.status
    return res


@trust_partners_bp.route("", methods=["GET"])
@jwt_required()
def list_partners():
    user = _seller()
    if user is None:
        return jsonify({"message": "Only sellers with a store have a Passport."}), 403
    return jsonify({
        "platforms": evidence.links_for_vendor(user.id),
        "explainer": ("Other platforms can report your completed orders to your Trust Passport. "
                      "Their reports count only after you accept them, count less than sales on Siiqo, "
                      "and you can stop them at any time."),
        "max_partner_weight": trust_v1.PARTNER_RULES["max_weight"],
    }), 200


@trust_partners_bp.route("/<int:client_id>/<action>", methods=["POST"])
@jwt_required()
def set_partner(client_id, action):
    user = _seller()
    if user is None:
        return jsonify({"message": "Only sellers with a store have a Passport."}), 403
    if action not in ("accept", "revoke"):
        return jsonify({"code": "NOT_FOUND", "message": "Not found."}), 404
    data = request.get_json(silent=True) or {}
    try:
        evidence.set_link(user.id, client_id, action, disputed=bool(data.get("disputed")))
    except evidence.EvidenceError as e:
        return _error(e)
    item = next((p for p in evidence.links_for_vendor(user.id) if p["client_id"] == client_id), None)
    return jsonify({"platform": item}), 200


# ── superadmin ─────────────────────────────────────────────────────────────

def _superadmin():
    from app.routes.admin import _parse_admin_id, _require_superadmin
    return _require_superadmin(_parse_admin_id(get_jwt_identity()))


@evidence_admin_bp.route("", methods=["GET"])
@jwt_required()
def admin_list():
    if not _superadmin():
        return jsonify({"message": "Superadmin access required"}), 403
    return jsonify({"partners": evidence.admin_overview(), "rules": trust_v1.PARTNER_RULES,
                    "default_throttle_factor": evidence.DEFAULT_THROTTLE_FACTOR}), 200


@evidence_admin_bp.route("/<int:client_id>/<action>", methods=["POST"])
@jwt_required()
def admin_control(client_id, action):
    admin = _superadmin()
    if not admin:
        return jsonify({"message": "Superadmin access required"}), 403
    from app.models.passport import PassportApiClient
    if action not in ("throttle", "revoke", "restore"):
        return jsonify({"code": "NOT_FOUND", "message": "Not found."}), 404
    if db.session.get(PassportApiClient, client_id) is None:
        return jsonify({"code": "NOT_FOUND", "message": "No such API key."}), 404
    data = request.get_json(silent=True) or {}
    try:
        evidence.set_control(client_id, action, admin, reason=data.get("reason"), factor=data.get("factor"))
    except evidence.EvidenceError as e:
        db.session.rollback()
        return _error(e)
    item = next((p for p in evidence.admin_overview() if p["client_id"] == client_id), None)
    status, factor = evidence.control_state(client_id)
    return jsonify({"partner": item or {"client_id": client_id, "status": status, "factor": factor}}), 200
