"""
Account preferences (/api/account/*).

  GET /locale   -> {locale, preferred_locale, enabled}
  PUT /locale   {locale: "en" | null} -> same shape

`preferred_locale` is the saved setting (null = not chosen); `locale` is what
Siiqo will actually use (English unless the saved language is enabled in
LANGUAGES). English only for now (D19), so only "en" or null can be saved.
"""
from flask import Blueprint, jsonify, request
from flask_jwt_extended import get_jwt_identity, jwt_required

from app.extensions import db, limiter
from app.i18n import enabled_locales, normalize_locale, user_locale
from app.models.user import User

account_bp = Blueprint("account", __name__)


def _current_user():
    identity = get_jwt_identity()
    if not identity or not str(identity).isdigit():
        return None  # admin tokens ("admin:N") have no account preferences here
    user = db.session.get(User, int(identity))
    return user if user and user.is_active else None


def _out(user):
    return {"locale": user_locale(user), "preferred_locale": user.preferred_locale,
            "enabled": list(enabled_locales())}


@account_bp.route("/locale", methods=["GET"])
@jwt_required()
def get_locale():
    user = _current_user()
    if not user:
        return jsonify({"message": "Unauthorized"}), 403
    return jsonify(_out(user)), 200


@account_bp.route("/locale", methods=["PUT"])
@jwt_required()
@limiter.limit("30 per hour")
def set_locale():
    user = _current_user()
    if not user:
        return jsonify({"message": "Unauthorized"}), 403
    data = request.get_json(silent=True) or {}
    if "locale" not in data:
        return jsonify({"message": "locale is required (or null to clear it)", "code": "LOCALE_REQUIRED"}), 400
    raw = data.get("locale")
    if raw is None:
        user.preferred_locale = None
    else:
        code = normalize_locale(raw)
        if code is None:
            return jsonify({"message": "Unknown language", "code": "UNKNOWN_LOCALE"}), 400
        if code not in enabled_locales():
            return jsonify({"message": "This language isn't available yet", "code": "LOCALE_NOT_AVAILABLE",
                            "enabled": list(enabled_locales())}), 400
        user.preferred_locale = code
    db.session.commit()
    return jsonify(_out(user)), 200
