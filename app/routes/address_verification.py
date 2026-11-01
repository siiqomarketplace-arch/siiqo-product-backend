"""
Address verification API (V5): /api/verification/address/*

  GET  /status    the seller's address check status (coming_soon while off)
  POST /start     {nin, address{building_number, street, landmark, city, state,
                   lga?, flat_number?, building_name?, sub_street?}, consent: true}
  POST /refresh   ask the provider for news on a pending check

Hidden behind ADDRESS_VERIFICATION_ENABLED (off by default). Responses never
contain the address or the NIN.
"""
from flask import Blueprint, jsonify, request
from flask_jwt_extended import get_jwt_identity, jwt_required

from app.extensions import db, limiter
from app.models.user import User
from app.services.address_verification import service as addr

address_verification_bp = Blueprint("address_verification", __name__)


def _current_user():
    identity = get_jwt_identity()
    if not identity or not str(identity).isdigit():
        return None  # admin tokens ("admin:N") are not sellers
    user = db.session.get(User, int(identity))
    return user if user and user.is_active else None


def _error(exc: addr.AddressVerificationError):
    return jsonify({"message": exc.message, "code": exc.code}), exc.status


@address_verification_bp.route("/status", methods=["GET"])
@jwt_required()
def get_status():
    user = _current_user()
    if not user:
        return jsonify({"message": "Seller account required"}), 403
    return jsonify(addr.seller_summary(user)), 200


@address_verification_bp.route("/start", methods=["POST"])
@jwt_required()
@limiter.limit("5 per hour")
def start():
    user = _current_user()
    if not user:
        return jsonify({"message": "Seller account required"}), 403
    data = request.get_json(silent=True) or {}
    try:
        addr.start(user, nin=str(data.get("nin") or ""), address=data.get("address"),
                   consent=data.get("consent") is True)
    except addr.AddressVerificationError as exc:
        return _error(exc)
    return jsonify(addr.seller_summary(user)), 201


@address_verification_bp.route("/refresh", methods=["POST"])
@jwt_required()
@limiter.limit("20 per hour")
def refresh():
    user = _current_user()
    if not user:
        return jsonify({"message": "Seller account required"}), 403
    if not addr.is_enabled():
        return jsonify({"message": "Address verification is coming soon.", "code": "COMING_SOON"}), 404
    row = addr.latest(user.id)
    if row is not None:
        addr.refresh(row, force=True)
    return jsonify(addr.seller_summary(user)), 200
