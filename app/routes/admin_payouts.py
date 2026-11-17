"""
Payouts recorded as "paid by Paystack split" for orders in a multi-seller
payment, where no split could have happened (issue B1). Registered on the
admin blueprint (imported at the bottom of app/routes/admin.py). No gateway
calls: restoring only writes a ledger CREDIT so the seller can withdraw.

    GET  /api/admin/payouts/split-mismatch                    list + total (superadmin)
    POST /api/admin/payouts/split-mismatch/<order_id>/restore {note}  (superadmin, audit-logged)
"""
from flask import jsonify, request
from flask_jwt_extended import get_jwt_identity, jwt_required

from app.extensions import db
from app.models.audit import AdminAuditLog
from app.routes.admin import _client_ip, _parse_admin_id, _require_superadmin, admin_bp
from app.services import split_settlement as svc


def _superadmin():
    try:
        return _require_superadmin(_parse_admin_id(get_jwt_identity()))
    except (ValueError, TypeError):
        return None


@admin_bp.route("/payouts/split-mismatch", methods=["GET"])
@jwt_required()
def admin_split_mismatch_list():
    if not _superadmin():
        return jsonify({"message": "Superadmin access required"}), 403
    rows = svc.split_mismatches()
    return jsonify({"status": "success", "orders": rows, "total_amount": svc.mismatch_total(rows),
                    "count": sum(1 for r in rows if not r["restored"])}), 200


@admin_bp.route("/payouts/split-mismatch/<int:order_id>/restore", methods=["POST"])
@jwt_required()
def admin_split_mismatch_restore(order_id):
    admin = _superadmin()
    if not admin:
        return jsonify({"message": "Superadmin access required"}), 403
    note = ((request.get_json(silent=True) or {}).get("note") or "").strip()[:500]
    row, created = svc.restore_mismatch(order_id)
    if row is None:
        return jsonify({"message": f"Order #{order_id} is not on the split-mismatch list."}), 404
    if created:
        AdminAuditLog.log_action(
            admin=admin, action="PAYOUT_SPLIT_MISMATCH_RESTORED", resource_type="Order",
            resource_id=order_id,
            details={"vendor_id": row["vendor"]["id"], "amount": row["amount"],
                     "reference": svc.restore_reference(order_id), "note": note},
            ip_address=_client_ip(),
        )
        db.session.commit()
        return jsonify({"status": "success", "message": f"₦{float(row['amount']):,.2f} is back in the seller's balance.",
                        "order": row}), 200
    return jsonify({"status": "success", "message": "Already restored. Nothing changed.", "order": row}), 200
