"""
Admin: Delivery money (app/services/delivery_money.py, partner_payouts.py).

    GET  /api/admin/delivery-money?from=YYYY-MM-DD&to=YYYY-MM-DD
         delivery fees collected from buyers, owed to / paid to partners,
         owed to sellers for their own delivery, Siiqo's delivery margin,
         one row per partner, legacy Pay on Delivery partner jobs, and the
         weekly automatic payout runs                                   (admin)
    GET  /api/admin/delivery-money/seller-fees-unpaid
         orders released before sellers were paid their own delivery fee (admin)
    POST /api/admin/delivery-money/seller-fees-unpaid/<order_id>/credit {note}
         credit that fee to the seller's balance: ledger CREDIT with the fixed
         reference delivery_fee:<order_id> (idempotent)   (superadmin, audited)

No gateway calls here: crediting writes a ledger row the seller withdraws.
"""
from flask import Blueprint, jsonify, request
from flask_jwt_extended import get_jwt_identity, jwt_required

from app.extensions import db
from app.models.audit import AdminAuditLog
from app.services import delivery_money as svc

delivery_money_admin_bp = Blueprint("delivery_money_admin", __name__)


def _admin():
    from app.routes.admin import _get_admin
    return _get_admin(get_jwt_identity())


def _superadmin():
    from app.routes.admin import _parse_admin_id, _require_superadmin
    try:
        return _require_superadmin(_parse_admin_id(get_jwt_identity()))
    except (ValueError, TypeError):
        return None


@delivery_money_admin_bp.route("", methods=["GET"])
@jwt_required()
def delivery_money_report():
    if not _admin():
        return jsonify({"message": "Admin access required"}), 403
    from app.models.withdrawal import PartnerPayoutRun
    from app.services.partner_payouts import auto_payout_enabled
    start, end = svc.parse_period(request.args.get("from"), request.args.get("to"))
    report = svc.delivery_money_report(start, end)
    runs = PartnerPayoutRun.query.order_by(PartnerPayoutRun.id.desc()).limit(12).all()
    report["auto_payouts"] = {
        "enabled": auto_payout_enabled(),
        "schedule": "Fridays 09:00 Lagos time",
        "switch": "PARTNER_AUTO_PAYOUT_ENABLED",
        "runs": [r.to_dict() for r in runs],
    }
    report["refund_rule"] = svc.REFUND_RULE_TEXT
    return jsonify({"status": "success", **report}), 200


@delivery_money_admin_bp.route("/seller-fees-unpaid", methods=["GET"])
@jwt_required()
def seller_fees_unpaid():
    if not _admin():
        return jsonify({"message": "Admin access required"}), 403
    rows = svc.unpaid_seller_delivery_fees()
    total = sum((svc._dec(r["delivery_fee"]) for r in rows), svc._dec(0))
    return jsonify({"status": "success", "orders": rows, "count": len(rows), "total_amount": str(total)}), 200


@delivery_money_admin_bp.route("/seller-fees-unpaid/<int:order_id>/credit", methods=["POST"])
@jwt_required()
def credit_seller_fee(order_id):
    admin = _superadmin()
    if not admin:
        return jsonify({"message": "Superadmin access required"}), 403
    note = ((request.get_json(silent=True) or {}).get("note") or "").strip()[:500]
    row, created = svc.credit_unpaid_seller_delivery(order_id)
    if row is None:
        return jsonify({"message": f"Order #{order_id} has no unpaid seller delivery fee."}), 404
    if not created:
        return jsonify({"status": "success", "message": "Already credited. Nothing changed.", "order": row}), 200
    from app.routes.admin import _client_ip
    db.session.commit()
    AdminAuditLog.log_action(
        admin=admin, action="SELLER_DELIVERY_FEE_CREDITED", resource_type="Order", resource_id=order_id,
        details={**row, "note": note}, ip_address=_client_ip(),
    )
    db.session.commit()
    return jsonify({"status": "success",
                    "message": f"{svc.naira(row['delivery_fee'])} is now in the seller's balance.",
                    "order": row}), 200
