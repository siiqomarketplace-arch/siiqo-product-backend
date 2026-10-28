"""
Verification v2 API (/api/verification/*).

Hidden behind VERIFICATION_V2_ENABLED. Until launch, only users listed in
VERIFICATION_V2_ALLOWLIST can use it; everyone else sees "coming soon".
"""
import logging

from flask import Blueprint, jsonify, request
from flask_jwt_extended import get_jwt_identity, jwt_required

from app.extensions import db, limiter
from app.models.user import User
from app.models.verification import VerificationCase, VerificationKind
from app.services.verification import get_provider
from app.services.verification import service as ver

logger = logging.getLogger(__name__)
verification_bp = Blueprint("verification", __name__)


def _current_user():
    identity = get_jwt_identity()
    if not identity or not str(identity).isdigit():
        return None  # admin tokens ("admin:N") are not sellers
    return db.session.get(User, int(identity))


def _error(exc: ver.VerificationError):
    return jsonify({"message": exc.message, "code": exc.code}), exc.status


@verification_bp.route("/status", methods=["GET"])
@jwt_required()
def get_status():
    user = _current_user()
    if not user:
        return jsonify({"message": "Seller account required"}), 403
    return jsonify(ver.seller_summary(user)), 200


@verification_bp.route("/identity/start", methods=["POST"])
@jwt_required()
@limiter.limit("10 per hour")
def start_identity():
    user = _current_user()
    if not user:
        return jsonify({"message": "Seller account required"}), 403
    data = request.get_json() or {}
    try:
        case = ver.start_identity(user, seller_type=data.get("seller_type"),
                                  nin=str(data.get("nin") or ""),
                                  consent=data.get("consent") is True)
    except ver.VerificationError as exc:
        return _error(exc)
    return jsonify({"case": case.to_seller_dict()}), 201


@verification_bp.route("/cases/<int:case_id>", methods=["GET"])
@jwt_required()
def get_case(case_id):
    user = _current_user()
    case = db.session.get(VerificationCase, case_id)
    if not user or not case or case.user_id != user.id:
        return jsonify({"message": "Not found"}), 404
    if case.kind == VerificationKind.IDENTITY:
        case = ver.refresh_identity_case(case)  # polling fallback if the webhook is late
    return jsonify({"case": case.to_seller_dict()}), 200


@verification_bp.route("/business/verify", methods=["POST"])
@jwt_required()
@limiter.limit("10 per hour")
def verify_business():
    user = _current_user()
    if not user:
        return jsonify({"message": "Seller account required"}), 403
    data = request.get_json() or {}
    try:
        case = ver.verify_business(user, reg_number=str(data.get("reg_number") or ""),
                                   business_type=data.get("business_type"))
    except ver.VerificationError as exc:
        return _error(exc)
    return jsonify({"case": case.to_seller_dict()}), 200


@verification_bp.route("/webhook/<provider_name>", methods=["POST"])
def provider_webhook(provider_name):
    provider = get_provider()
    if provider_name != provider.name:
        return jsonify({"message": "Unknown provider"}), 404
    raw = request.get_data()
    if not provider.verify_webhook(raw, request.headers):
        logger.warning("[VERIFICATION WEBHOOK] rejected: bad or missing signature")
        return jsonify({"message": "Invalid signature"}), 401

    result = provider.parse_identity_webhook(request.get_json(silent=True) or {})
    if not result:
        return jsonify({"status": "ignored"}), 200
    case = VerificationCase.query.filter_by(provider_ref=result.provider_ref).first()
    if not case:
        return jsonify({"status": "unknown_case"}), 200
    ver.apply_identity_result(case, result)
    return jsonify({"status": "ok"}), 200


@verification_bp.route("/mock/complete", methods=["POST"])
@jwt_required()
def mock_complete():
    """Staff testing only: finish a mock hosted session (stands in for the
    provider's selfie screen). Refused unless the mock provider is active."""
    provider = get_provider()
    if provider.name != "mock":
        return jsonify({"message": "Not available"}), 404
    user = _current_user()
    if not user or not ver.is_available(user):
        return jsonify({"message": "Not available"}), 404
    ref = (request.get_json() or {}).get("ref") or ""
    case = VerificationCase.query.filter_by(provider_ref=ref).first()
    if not case or case.user_id != user.id:
        return jsonify({"message": "Not found"}), 404
    case = ver.refresh_identity_case(case)
    return jsonify({"case": case.to_seller_dict()}), 200
