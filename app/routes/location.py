"""
Shop location API (NIPOST postcode): /api/location

  GET    /status   {enabled, needed, consent_given, location | null}
  POST   /         {postcode, consent: true, method?}  validate + save
  DELETE /         remove now (postcode fields cleared; audit row kept)

All answer 404 FEATURE_OFF while POSTCODE_VERIFICATION_ENABLED is off.
"""
from flask import Blueprint, jsonify, request
from flask_jwt_extended import get_jwt_identity, jwt_required

from app.extensions import db
from app.models.user import User
from app.services import shop_location as loc
from app.services.postcode import PostcodeError

location_bp = Blueprint("location", __name__)


def _current_user():
    identity = get_jwt_identity()
    if not identity or not str(identity).isdigit():
        return None  # admin tokens are not sellers
    user = db.session.get(User, int(identity))
    return user if user and user.is_active else None


def _error(exc: PostcodeError):
    return jsonify({"message": exc.message, "code": exc.code}), exc.status


def _off():
    return jsonify({"enabled": False, "code": "FEATURE_OFF",
                    "message": "Shop location is not available yet."}), 404


@location_bp.route("/status", methods=["GET"])
@jwt_required()
def status():
    if not loc.is_enabled():
        return _off()
    user = _current_user()
    if not user:
        return jsonify({"message": "Seller account required"}), 403
    return jsonify(loc.seller_status(user)), 200


@location_bp.route("", methods=["POST"])
@jwt_required()
def save():
    if not loc.is_enabled():
        return _off()
    user = _current_user()
    if not user:
        return jsonify({"message": "Seller account required"}), 403
    data = request.get_json(silent=True) or {}
    try:
        loc.save(user, postcode=data.get("postcode"), consent=data.get("consent") is True,
                 method=data.get("method"))
    except PostcodeError as exc:
        return _error(exc)
    return jsonify(loc.seller_status(user)), 201


@location_bp.route("", methods=["DELETE"])
@jwt_required()
def remove():
    if not loc.is_enabled():
        return _off()
    user = _current_user()
    if not user:
        return jsonify({"message": "Seller account required"}), 403
    loc.remove(user)
    return jsonify(loc.seller_status(user)), 200
