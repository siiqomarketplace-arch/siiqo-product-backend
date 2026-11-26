"""
Payouts recorded as paid with no proof (Order 639 and the multi-seller
Paystack carts before it), for every gateway, single or multi-seller.
Registered on the admin blueprint (imported at the bottom of app/routes/admin.py).
Nothing here sends money: restore writes one ledger CREDIT, a manual payout
only records a transfer a person already sent.

    GET  /api/admin/payouts/split-mismatch?page=&per_page=&include_untracked=1
                                                         list (superadmin, paginated)
    POST /api/admin/payouts/split-mismatch/<order_id>/restore        {note}
    POST /api/admin/payouts/split-mismatch/<order_id>/manual-payout
         {amount, gateway, reference, paid_on: YYYY-MM-DD, note}
    POST /api/admin/payouts/split-mismatch/<order_id>/check          Paystack only
    POST /api/admin/payouts/flutterwave/fix-subaccount-splits        Siiqo 3% on every subaccount
All POSTs: superadmin, audit-logged, idempotent.
"""
import logging

from flask import jsonify, request
from flask_jwt_extended import get_jwt_identity, jwt_required

from app.extensions import db
from app.models.audit import AdminAuditLog
from app.routes.admin import _client_ip, _parse_admin_id, _require_superadmin, admin_bp
from app.services import split_settlement as svc

log = logging.getLogger(__name__)


def _superadmin():
    try:
        return _require_superadmin(_parse_admin_id(get_jwt_identity()))
    except (ValueError, TypeError):
        return None


def _truthy(v) -> bool:
    return str(v or "").strip().lower() in ("1", "true", "yes", "on")


@admin_bp.route("/payouts/split-mismatch", methods=["GET"])
@jwt_required()
def admin_split_mismatch_list():
    if not _superadmin():
        return jsonify({"message": "Superadmin access required"}), 403
    try:
        res = svc.split_mismatches(page=request.args.get("page", 1, type=int),
                                   per_page=request.args.get("per_page", 50, type=int),
                                   include_untracked=_truthy(request.args.get("include_untracked")))
    except Exception as exc:  # noqa: BLE001
        log.error("[PAYOUT CHECK] list failed: %s", exc, exc_info=True)
        db.session.rollback()
        return jsonify({"message": "The payout check failed on the server. Try again, or check the logs."}), 500
    rows = res["orders"]
    return jsonify({"status": "success", "orders": rows, "total_amount": svc.mismatch_total(rows),
                    "count": sum(1 for r in rows if not r["restored"]), "total": res["total"],
                    "page": res["page"], "pages": res["pages"], "per_page": res["per_page"],
                    "capped": res["capped"]}), 200


@admin_bp.route("/payouts/split-mismatch/<int:order_id>/restore", methods=["POST"])
@jwt_required()
def admin_split_mismatch_restore(order_id):
    admin = _superadmin()
    if not admin:
        return jsonify({"message": "Superadmin access required"}), 403
    note = ((request.get_json(silent=True) or {}).get("note") or "").strip()[:500]
    row, created = svc.restore_mismatch(order_id)
    if row is None:
        return jsonify({"message": f"Order #{order_id} is not on the payout check list."}), 404
    if created:
        AdminAuditLog.log_action(
            admin=admin, action="PAYOUT_SPLIT_MISMATCH_RESTORED", resource_type="Order",
            resource_id=order_id,
            details={"vendor_id": row["vendor"]["id"], "amount": row["amount"], "gateway": row.get("gateway"),
                     "reference": svc.restore_reference(order_id), "note": note},
            ip_address=_client_ip(),
        )
        db.session.commit()
        return jsonify({"status": "success", "message": f"₦{float(row['amount']):,.2f} is back in the seller's balance.",
                        "order": row}), 200
    return jsonify({"status": "success", "message": "Already restored. Nothing changed.", "order": row}), 200


@admin_bp.route("/payouts/split-mismatch/<int:order_id>/manual-payout", methods=["POST"])
@jwt_required()
def admin_record_manual_payout(order_id):
    admin = _superadmin()
    if not admin:
        return jsonify({"message": "Superadmin access required"}), 403
    data = request.get_json(silent=True) or {}
    try:
        rec, created = svc.record_manual_payout(
            order_id, amount=data.get("amount"), gateway=data.get("gateway"),
            reference=data.get("reference"), paid_on=data.get("paid_on"), note=data.get("note") or "",
            admin_id=admin.id)
    except svc.ManualPayoutError as exc:
        db.session.rollback()
        return jsonify({"message": str(exc)}), 400
    if created:
        AdminAuditLog.log_action(
            admin=admin, action="PAYOUT_MANUAL_RECORDED", resource_type="Order", resource_id=order_id,
            details=rec.to_dict(), ip_address=_client_ip(),
        )
        db.session.commit()
        return jsonify({"status": "success", "message": f"Manual payout recorded for Order #{order_id}.",
                        "manual_payout": rec.to_dict()}), 200
    return jsonify({"status": "success", "message": "Already recorded. Nothing changed.",
                    "manual_payout": rec.to_dict()}), 200


@admin_bp.route("/payouts/split-mismatch/<int:order_id>/check", methods=["POST"])
@jwt_required()
def admin_split_mismatch_check(order_id):
    admin = _superadmin()
    if not admin:
        return jsonify({"message": "Superadmin access required"}), 403
    res = svc.check_with_gateway(order_id)
    if res.get("checked"):
        AdminAuditLog.log_action(admin=admin, action="PAYOUT_SPLIT_CHECKED", resource_type="Order",
                                 resource_id=order_id, details=res, ip_address=_client_ip())
    db.session.commit()
    return jsonify({"status": "success" if res.get("checked") else "error", **res}), 200 if res.get("checked") else 400


@admin_bp.route("/payouts/flutterwave/fix-subaccount-splits", methods=["POST"])
@jwt_required()
def admin_fix_flw_subaccount_splits():
    """Set Siiqo's commission on every Flutterwave subaccount to 3%
    (split_type percentage, split_value 0.03). Already-correct ones are skipped."""
    admin = _superadmin()
    if not admin:
        return jsonify({"message": "Superadmin access required"}), 403
    from app.services import flutterwave_service as flw
    if not flw.is_configured():
        return jsonify({"message": "Flutterwave is not configured."}), 400
    res = flw.fix_subaccount_splits()
    AdminAuditLog.log_action(admin=admin, action="FLW_SUBACCOUNT_SPLITS_FIXED", resource_type="Flutterwave",
                             resource_id=None,
                             details={"seen": res.get("seen"), "fixed": res.get("fixed"),
                                      "skipped": res.get("skipped"), "failed": res.get("failed"),
                                      "error": res.get("error_message")},
                             ip_address=_client_ip())
    db.session.commit()
    msg = (f"{len(res.get('fixed') or [])} fixed, {res.get('skipped', 0)} already right, "
           f"{len(res.get('failed') or [])} failed.")
    if res.get("error_message"):
        msg = f"Stopped: {res['error_message']}. " + msg
    return jsonify({"status": "success" if res.get("success") else "error", "message": msg, **res}), \
        200 if res.get("success") else 502
