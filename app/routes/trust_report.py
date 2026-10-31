"""
Trust Report and "Who checked you", mounted beside /check at /api/check.

    GET /api/check/report/<slug>             verdict, reasons, Safe Pay advice (rules; no AI; fast)
    GET /api/check/report/<slug>/narrative   AI narrative (cached by evidence hash; template fallback)
    GET /api/check/insights                  the signed-in seller's own "Who checked you" counts

Reports exist only for sellers whose Trust Passport is public. Unknown
businesses keep /check's neutral not-found wording.
"""
from flask import Blueprint, jsonify
from flask_jwt_extended import get_jwt_identity, jwt_required

from app.extensions import get_real_client_ip, limiter
from app.services import who_checked
from app.services import passport as passport_svc
from app.services import trust_report

trust_report_bp = Blueprint('trust_report', __name__)

VERDICT_RATE_LIMIT = "30 per minute"
NO_REPORT = "No public Trust Passport for this seller."


def _visible_store(slug):
    sf = passport_svc.find_storefront(slug)
    return sf if passport_svc.is_visible(sf) else None


def _narrative_allowed():
    """Free tier: TRUST_REPORT_NARRATIVES_PER_DAY (default 5) new AI narratives
    per IP per day. Cached narratives and the template never count. The IP is
    used only as the limiter key; it is never stored in the database."""
    if not limiter.enabled:
        return True
    from limits import RateLimitItemPerDay
    item = RateLimitItemPerDay(trust_report.daily_narrative_limit())
    try:
        return limiter.limiter.hit(item, "trust_report_narrative", get_real_client_ip())
    except Exception:  # noqa: BLE001 - limiter storage down: fail closed (template only)
        return False


@trust_report_bp.route('/report/<slug>', methods=['GET'])
@limiter.limit(VERDICT_RATE_LIMIT)
def report(slug):
    sf = _visible_store(slug)
    if sf is None:
        return jsonify({"message": NO_REPORT}), 404
    body = trust_report.verdict(trust_report.gather(sf))
    res = jsonify(body)
    res.headers["Cache-Control"] = "no-store"
    return res, 200


@trust_report_bp.route('/report/<slug>/narrative', methods=['GET'])
@limiter.limit(VERDICT_RATE_LIMIT)
def report_narrative(slug):
    sf = _visible_store(slug)
    if sf is None:
        return jsonify({"message": NO_REPORT}), 404
    body = trust_report.narrative(sf, allow_generation=_narrative_allowed)
    res = jsonify(body)
    res.headers["Cache-Control"] = "no-store"
    return res, 200


@trust_report_bp.route('/insights', methods=['GET'])
@jwt_required()
@limiter.limit("60 per minute")
def my_insights():
    """Only the signed-in seller's own counts; there is no way to ask for another seller's."""
    from app.models.user import Storefront
    try:
        user_id = int(get_jwt_identity())
    except (TypeError, ValueError):
        return jsonify({"message": "Seller account required"}), 403
    sf = Storefront.query.filter_by(vendor_id=user_id).first()
    if sf is None:
        return jsonify({"message": "Create your store to see who checked you."}), 404
    return jsonify(who_checked.for_vendor(user_id)), 200
