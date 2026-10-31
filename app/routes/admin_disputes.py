"""
Admin dispute review (Phase 2 item 2-5). Registered on the admin blueprint
(imported at the bottom of app/routes/admin.py): /api/admin/disputes/...

    GET  /api/admin/disputes?status=        list (any admin)
    GET  /api/admin/disputes/<id>           case, evidence, summary, money (any admin)
    POST /api/admin/disputes/<id>/review    close evidence early, queue the summary (any admin)
    POST /api/admin/disputes/<id>/summary   re-run the summary (any admin)
    POST /api/admin/disputes/<id>/decide    the decision (superadmin, 2FA enforced, audit-logged)
    POST /api/admin/disputes/<id>/refund-sent  record a manual refund (superadmin, audit-logged)
"""
from flask import jsonify, request
from flask_jwt_extended import get_jwt_identity, jwt_required

from app.extensions import db, limiter
from app.models.dispute import DisputeCase, DisputeStatus
from app.routes.admin import _client_ip, _get_admin, _parse_admin_id, _require_superadmin, admin_bp
from app.services import disputes as svc


def _admin():
    return _get_admin(_parse_admin_id(get_jwt_identity()))


def _superadmin():
    return _require_superadmin(_parse_admin_id(get_jwt_identity()))


def _err(e: svc.DisputeError):
    return jsonify(e.to_dict()), e.status


@admin_bp.route("/disputes", methods=["GET"])
@jwt_required()
def admin_list_disputes():
    if not _admin():
        return jsonify({"message": "Unauthorized"}), 403
    svc.adopt_legacy_disputes()
    svc.sweep_deadlines()
    status = (request.args.get("status") or "").strip().lower()
    q = DisputeCase.query
    if status == "active":
        q = q.filter(DisputeCase.status.in_(DisputeStatus.ACTIVE))
    elif status == "refund_due":
        q = q.filter(DisputeCase.refund_status == "manual_due")
    elif status in DisputeStatus.ALL:
        q = q.filter(DisputeCase.status == status)
    cases = q.order_by(DisputeCase.created_at.desc()).limit(200).all()
    counts = {s: DisputeCase.query.filter_by(status=s).count() for s in DisputeStatus.ALL}
    counts["refund_due"] = DisputeCase.query.filter_by(refund_status="manual_due").count()
    return jsonify({"status": "success", "counts": counts,
                    "disputes": [svc.admin_view(c, with_evidence=False) for c in cases]}), 200


@admin_bp.route("/disputes/<int:case_id>", methods=["GET"])
@jwt_required()
def admin_get_dispute(case_id):
    if not _admin():
        return jsonify({"message": "Unauthorized"}), 403
    case = db.session.get(DisputeCase, case_id)
    if case is None:
        return jsonify({"message": "Dispute not found"}), 404
    return jsonify({"status": "success", "dispute": svc.admin_view(case)}), 200


@admin_bp.route("/disputes/<int:case_id>/review", methods=["POST"])
@jwt_required()
def admin_move_dispute_to_review(case_id):
    if not _admin():
        return jsonify({"message": "Unauthorized"}), 403
    case = db.session.get(DisputeCase, case_id)
    if case is None:
        return jsonify({"message": "Dispute not found"}), 404
    if not svc.move_to_review(case, "admin"):
        return jsonify({"message": f"The dispute is {case.status}; it can't be moved to review.",
                        "code": "not_open"}), 409
    return jsonify({"status": "success", "dispute": svc.admin_view(db.session.get(DisputeCase, case_id))}), 200


@admin_bp.route("/disputes/<int:case_id>/summary", methods=["POST"])
@jwt_required()
@limiter.limit("20 per hour")
def admin_rerun_dispute_summary(case_id):
    if not _admin():
        return jsonify({"message": "Unauthorized"}), 403
    case = db.session.get(DisputeCase, case_id)
    if case is None:
        return jsonify({"message": "Dispute not found"}), 404
    if case.status != DisputeStatus.UNDER_REVIEW:
        return jsonify({"message": "Summaries are only made for cases under review.", "code": "not_under_review"}), 409
    if case.summary_status == "pending":
        return jsonify({"message": "A summary is already being written.", "code": "pending"}), 409
    svc.queue_summary(case)
    return jsonify({"status": "success", "dispute": svc.admin_view(db.session.get(DisputeCase, case_id))}), 202


@admin_bp.route("/disputes/<int:case_id>/decide", methods=["POST"])
@jwt_required()
@limiter.limit("30 per hour")
def admin_decide_dispute(case_id):
    admin = _superadmin()
    if not admin:
        return jsonify({"message": "Superadmin access required"}), 403
    data = request.get_json(silent=True) or {}
    try:
        case, already = svc.decide(
            case_id, admin, (data.get("outcome") or "").strip(),
            seller_fault=data.get("seller_fault"),
            note=data.get("note"),
            refund_amount=data.get("refund_amount"),
            buyer_share_percent=data.get("buyer_share_percent"),
            ip=_client_ip(),
        )
    except svc.DisputeError as e:
        return _err(e)
    case = db.session.get(DisputeCase, case.id)
    return jsonify({"status": "success", "already_decided": already, "dispute": svc.admin_view(case)}), 200


@admin_bp.route("/disputes/<int:case_id>/refund-sent", methods=["POST"])
@jwt_required()
@limiter.limit("30 per hour")
def admin_dispute_refund_sent(case_id):
    admin = _superadmin()
    if not admin:
        return jsonify({"message": "Superadmin access required"}), 403
    data = request.get_json(silent=True) or {}
    try:
        case, already = svc.mark_refund_sent(case_id, admin, data.get("reference"), ip=_client_ip())
    except svc.DisputeError as e:
        return _err(e)
    case = db.session.get(DisputeCase, case.id)
    return jsonify({"status": "success", "already_recorded": already, "dispute": svc.admin_view(case)}), 200
