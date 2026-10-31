"""
trust_admin.py: Trust Score v1 shadow comparison (superadmin only).

  GET  /api/admin/trust/v1-comparison   old vs v1 per seller (latest shadow day),
                                        biggest differences, distribution
  GET  /api/admin/trust/v1/<vendor_id>  one seller's latest v1 row with its inputs
  POST /api/admin/trust/v1-run          run today's shadow computation now

Read-only for the public score: nothing here changes what buyers see. The old
score is on 0-1000 and v1 on 0-100, so differences compare old/10 with v1.
"""
import statistics
from datetime import datetime, timezone

from flask import Blueprint, jsonify, request
from flask_jwt_extended import get_jwt_identity, jwt_required

from app.extensions import db
from app.services import trust_v1

trust_admin_bp = Blueprint("trust_admin", __name__)

_TIER_ORDER = ["NEW", "BRONZE", "SILVER", "GOLD", "PLATINUM", "UNRANKED"]


def _superadmin():
    from app.routes.admin import _parse_admin_id, _require_superadmin
    return _require_superadmin(_parse_admin_id(get_jwt_identity()))


def _diff(row):
    if row.score is None or row.old_score is None:
        return None
    return round(row.score - row.old_score / 10.0, 1)


def _summary(values):
    if not values:
        return {"count": 0, "mean": None, "median": None}
    return {"count": len(values), "mean": round(statistics.mean(values), 1),
            "median": round(statistics.median(values), 1)}


@trust_admin_bp.route("/v1-comparison", methods=["GET"])
@jwt_required()
def v1_comparison():
    if not _superadmin():
        return jsonify({"message": "Superadmin access required"}), 403
    from app.models.trust import TrustScoreShadow
    from app.models.user import Storefront

    try:
        page = max(1, int(request.args.get("page", 1)))
        per_page = min(100, max(1, int(request.args.get("per_page", 25))))
    except ValueError:
        return jsonify({"message": "page and per_page must be numbers"}), 400

    day = (db.session.query(db.func.max(TrustScoreShadow.computed_on))
           .filter(TrustScoreShadow.version == trust_v1.VERSION).scalar())
    base = {"version": trust_v1.VERSION, "live": trust_v1.is_live(), "weights": trust_v1.WEIGHTS,
            "rules": trust_v1.RULES, "tiers": [{"min": m, "tier": t} for m, t in trust_v1.TIERS],
            "computed_on": day.isoformat() if day else None}
    if day is None:
        return jsonify({**base, "sellers": [], "biggest_differences": [], "distribution": None,
                        "total": 0, "page": page, "per_page": per_page}), 200

    rows = TrustScoreShadow.query.filter_by(version=trust_v1.VERSION, computed_on=day).all()
    stores = {sf.vendor_id: sf for sf in
              Storefront.query.filter(Storefront.vendor_id.in_([r.vendor_id for r in rows])).all()} if rows else {}

    def item(r):
        sf = stores.get(r.vendor_id)
        c = r.components or {}
        return {"vendor_id": r.vendor_id,
                "store_name": sf.store_name if sf else None,
                "store_slug": sf.store_slug if sf else None,
                "old_score": r.old_score, "old_tier": r.old_tier,
                "v1_score": r.score, "v1_tier": r.tier, "difference": _diff(r),
                "qualifying_sales": (c.get("minimum_history") or {}).get("qualifying_sales"),
                "anti_gaming": c.get("anti_gaming") or {}}

    def sort_key(r):
        d = _diff(r)
        return (d is None, -abs(d) if d is not None else 0, r.vendor_id)

    ordered = sorted(rows, key=sort_key)
    start = (page - 1) * per_page

    v1_tiers, old_tiers = {}, {}
    buckets = [0] * 10
    for r in rows:
        v1_tiers[r.tier] = v1_tiers.get(r.tier, 0) + 1
        old_tiers[r.old_tier or "NONE"] = old_tiers.get(r.old_tier or "NONE", 0) + 1
        if r.score is not None:
            buckets[min(9, r.score // 10)] += 1
    scored = [r for r in rows if r.score is not None]
    distribution = {
        "sellers": len(rows),
        "new_sellers": sum(1 for r in rows if r.score is None),
        "v1_tiers": {t: v1_tiers[t] for t in sorted(v1_tiers, key=lambda t: (_TIER_ORDER + [t]).index(t))},
        "old_tiers": {t: old_tiers[t] for t in sorted(old_tiers, key=lambda t: (_TIER_ORDER + [t]).index(t))},
        "v1_histogram": [{"from": i * 10, "to": i * 10 + 9 if i < 9 else 100, "count": n}
                         for i, n in enumerate(buckets)],
        "v1_scores": _summary([r.score for r in scored]),
        "old_scores_scaled": _summary([r.old_score / 10.0 for r in rows if r.old_score is not None]),
        "old_scores_scaled_where_v1_scored": _summary([r.old_score / 10.0 for r in scored
                                                       if r.old_score is not None]),
        "tier_changes": sum(1 for r in rows if r.old_tier and r.tier != r.old_tier),
    }
    return jsonify({
        **base,
        "distribution": distribution,
        "biggest_differences": [item(r) for r in ordered[:10] if _diff(r) is not None],
        "sellers": [item(r) for r in ordered[start:start + per_page]],
        "total": len(rows), "page": page, "per_page": per_page,
    }), 200


@trust_admin_bp.route("/v1/<int:vendor_id>", methods=["GET"])
@jwt_required()
def v1_seller(vendor_id):
    if not _superadmin():
        return jsonify({"message": "Superadmin access required"}), 403
    row = trust_v1.latest_row(vendor_id)
    if row is None:
        return jsonify({"message": "No v1 score stored for this seller yet"}), 404
    return jsonify(row.to_dict()), 200


@trust_admin_bp.route("/v1-run", methods=["POST"])
@jwt_required()
def v1_run():
    admin = _superadmin()
    if not admin:
        return jsonify({"message": "Superadmin access required"}), 403
    result = trust_v1.run_shadow()
    try:
        from app.models.audit import AdminAuditLog
        db.session.add(AdminAuditLog(
            admin_id=admin.id, admin_email=admin.email, admin_role=admin.role,
            action="TRUST_V1_SHADOW_RUN", resource_type="trust_score_shadow",
            resource_id=result["day"], details=result,
            ip_address=(request.remote_addr or "unknown")[:45],
            user_agent=request.headers.get("User-Agent"),
            timestamp=datetime.now(timezone.utc)))
        db.session.commit()
    except Exception:
        db.session.rollback()
    return jsonify(result), 200
