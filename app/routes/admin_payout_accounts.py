"""
Admin: payout accounts to check (app/services/payout_account_name.py).

    GET  /api/admin/payout-accounts             accounts with the name pending, a name
                                                equal to a bank or store name, or a
                                                confirmed name sharing no word with the
                                                seller's own name (warning only)
    POST /api/admin/payout-accounts/<id>/recheck  ask the bank now (Daya, then Paystack)

Account numbers are always masked here. Any signed-in admin may view and
re-check; re-checking only reads the bank's answer and never moves money.
"""
from flask import Blueprint, jsonify
from flask_jwt_extended import get_jwt_identity, jwt_required

from app.extensions import db
from app.models.audit import AdminAuditLog
from app.models.withdrawal import VendorBankAccount
from app.services import payout_account_name as svc

payout_accounts_admin_bp = Blueprint("payout_accounts_admin", __name__)


def _admin():
    from app.routes.admin import _get_admin
    return _get_admin(get_jwt_identity())


@payout_accounts_admin_bp.route("", methods=["GET"])
@jwt_required()
def list_payout_accounts():
    if not _admin():
        return jsonify({"message": "Admin access required"}), 403
    rows = svc.admin_list()
    return jsonify({
        "status": "success",
        "accounts": rows,
        "count": len(rows),
        "pending_count": sum(1 for r in rows if r["status"] == "pending"),
    }), 200


@payout_accounts_admin_bp.route("/<int:account_id>/recheck", methods=["POST"])
@jwt_required()
def recheck_payout_account(account_id):
    admin = _admin()
    if not admin:
        return jsonify({"message": "Admin access required"}), 403
    acc = db.session.get(VendorBankAccount, account_id)
    if acc is None:
        return jsonify({"message": "Account not found"}), 404
    result = svc.confirm_account(acc)
    if result["fixed"]:
        svc._notify_confirmed(acc)
    try:
        from app.routes.admin import _client_ip
        AdminAuditLog.log_action(
            admin=admin, action="PAYOUT_ACCOUNT_RECHECK", resource_type="VendorBankAccount",
            resource_id=acc.id,
            details={"vendor_id": acc.vendor_id, "result": result["status"],
                     "confirmed": result["confirmed"]},
            ip_address=_client_ip(),
        )
    except Exception:  # noqa: BLE001 (audit is best effort; the check itself stands)
        pass
    db.session.commit()
    message = (f"Confirmed by the bank: {acc.account_name}" if result["confirmed"]
               else "The bank didn't confirm a name. Still pending; we'll keep trying hourly.")
    return jsonify({"status": "success", "confirmed": result["confirmed"], "message": message,
                    "account": svc.admin_row(acc)}), 200
