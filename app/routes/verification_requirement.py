"""
Mandatory verification flag (D11, app/services/verification_requirement.py).

  GET /api/vendor/verification-requirement   the seller's one step ("Verify to
                                             keep selling" / "Verify to receive
                                             payouts above ₦X"), held payouts
  GET /api/admin/verification-requirement    superadmin: current mode and how
      ?mode=<value>                          many sellers a mode would affect,
                                             before switching it on
The flag itself is the env var VERIFICATION_REQUIRED_FOR; nothing here
changes it.
"""
from flask import Blueprint, jsonify, request
from flask_jwt_extended import get_jwt_identity, jwt_required

from app.extensions import db
from app.services import verification_requirement as requirement

verification_requirement_bp = Blueprint("verification_requirement", __name__)
verification_requirement_admin_bp = Blueprint("verification_requirement_admin", __name__)


@verification_requirement_bp.route("", methods=["GET"])
@jwt_required()
def seller_requirement():
    from app.models.user import User
    identity = str(get_jwt_identity() or "")
    user = db.session.get(User, int(identity)) if identity.isdigit() else None
    if user is None:
        return jsonify({"message": "Not found"}), 404
    return jsonify(requirement.seller_status(user.id)), 200


@verification_requirement_admin_bp.route("", methods=["GET"])
@jwt_required()
def admin_preview():
    from app.routes.admin import _parse_admin_id, _require_superadmin
    if not _require_superadmin(_parse_admin_id(get_jwt_identity())):
        return jsonify({"message": "Superadmin access required"}), 403
    raw = request.args.get("mode")
    if raw is not None and raw.strip() and requirement.parse_mode(raw)[0] == requirement.MODE_NONE \
            and raw.strip().lower() != requirement.MODE_NONE:
        return jsonify({"code": "BAD_MODE",
                        "message": "mode must be none, payouts_over:<amount>, new_stores or all_sellers"}), 400
    return jsonify(requirement.preview(raw if raw and raw.strip() else None)), 200
