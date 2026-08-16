"""
daya_admin.py — Admin API endpoints for Daya merchant balance and platform fee sweeps.

Routes:
  GET  /api/admin/daya/balance  — Live Daya balance + accumulated fees
  POST /api/admin/daya/sweep    — Manually trigger fee sweep to Siiqo corp bank
  GET  /api/admin/daya/sweeps   — List all sweep records (paginated)

All routes require SuperAdmin JWT.
"""

import logging
import os
import uuid
from datetime import datetime, timezone

from flask import Blueprint, request, jsonify
from flask_jwt_extended import jwt_required, get_jwt_identity

from app.extensions import db
from app.models.admin import AdminUser
from app.models.audit import AdminAuditLog
from app.models.escrow import EscrowTransaction, EscrowStatus
from sqlalchemy import func

daya_admin_bp = Blueprint("daya_admin", __name__)


def _utcnow():
    return datetime.now(timezone.utc)


def _get_admin(admin_id) -> AdminUser | None:
    return db.session.get(AdminUser, int(admin_id))


def _require_superadmin(admin_id) -> AdminUser | None:
    admin = _get_admin(admin_id)
    if not admin or admin.role != "SUPERADMIN":
        return None
    return admin


def _parse_admin_id(identity) -> int:
    """JWT identity may be a plain int or a dict with 'id' key."""
    if isinstance(identity, dict):
        return int(identity.get("id", 0))
    return int(identity)


# ---------------------------------------------------------------------------
# GET /api/admin/daya/balance
# ---------------------------------------------------------------------------

@daya_admin_bp.route("/balance", methods=["GET"])
@jwt_required()
def get_daya_balance():
    """
    Returns Siiqo's live Daya merchant balance (collection + withdrawal),
    the current NGN/USD FX rate, accumulated platform fees not yet swept,
    and the auto-sweep threshold + status.
    """
    from app.services import daya_service
    from app.models.fee_sweep import SiiqoFeeSweep

    admin_id = get_jwt_identity()
    if not _require_superadmin(_parse_admin_id(admin_id)):
        return jsonify({"message": "SuperAdmin required"}), 403

    try:
        # Live Daya balance
        raw = daya_service.get_merchant_balance()
        bal = raw.get("data", {})
        collection_usd = float(bal.get("collection_balance_usd", 0))
        withdrawal_usd = float(bal.get("withdrawal_balance_usd", 0))

        # Live FX rate
        try:
            rate_info = daya_service.get_rate(asset="USDT", side="SELL")
            fx_rate = float(rate_info.get("rate", 1500.0))
        except Exception:
            fx_rate = 1500.0

        collection_ngn = round(collection_usd * fx_rate, 2)
        withdrawal_ngn = round(withdrawal_usd * fx_rate, 2)

        # Accumulated Siiqo platform fees not yet swept
        total_fees_ngn = float(
            db.session.query(func.sum(EscrowTransaction.fee_amount))
            .filter(EscrowTransaction.status == EscrowStatus.RELEASED)
            .scalar() or 0
        )
        total_swept_ngn = float(
            db.session.query(func.sum(SiiqoFeeSweep.amount_ngn))
            .filter(SiiqoFeeSweep.status == "SUCCESS")
            .scalar() or 0
        )
        accumulated_ngn = max(total_fees_ngn - total_swept_ngn, 0.0)

        threshold = float(os.environ.get("SIIQO_FEE_SWEEP_THRESHOLD", "20000"))
        sweep_status = "READY_TO_SWEEP" if accumulated_ngn >= threshold else "ACCUMULATING"

        # Most recent successful sweep
        last_sweep = (
            SiiqoFeeSweep.query
            .filter_by(status="SUCCESS")
            .order_by(SiiqoFeeSweep.completed_at.desc())
            .first()
        )

        return jsonify({
            "status": "success",
            "data": {
                "collection_balance_usd": collection_usd,
                "collection_balance_ngn": collection_ngn,
                "withdrawal_balance_usd": withdrawal_usd,
                "withdrawal_balance_ngn": withdrawal_ngn,
                "current_fx_rate_ngn_per_usd": fx_rate,
                "accumulated_siiqo_fees_ngn": round(accumulated_ngn, 2),
                "total_fees_earned_ngn": round(total_fees_ngn, 2),
                "total_swept_ngn": round(total_swept_ngn, 2),
                "auto_sweep_threshold_ngn": threshold,
                "auto_sweep_status": sweep_status,
                "last_sweep_at": last_sweep.completed_at.isoformat() if last_sweep else None,
                "last_sweep_amount_ngn": str(last_sweep.amount_ngn) if last_sweep else None,
                "last_sweep_ref": last_sweep.reference if last_sweep else None,
                "corp_account_configured": bool(
                    os.environ.get("SIIQO_CORP_BANK_CODE")
                    and os.environ.get("SIIQO_CORP_ACCOUNT_NUMBER")
                ),
            },
        }), 200

    except Exception as exc:
        logging.error("[ADMIN DAYA BALANCE] %s", exc, exc_info=True)
        return jsonify({"message": f"Failed to fetch Daya balance: {exc}"}), 500


# ---------------------------------------------------------------------------
# POST /api/admin/daya/sweep
# ---------------------------------------------------------------------------

@daya_admin_bp.route("/sweep", methods=["POST"])
@jwt_required()
def trigger_fee_sweep():
    """
    Manually trigger an immediate platform fee sweep to Siiqo's corporate bank.
    Optional body: { "amount_ngn": 15000.00 }
    Defaults to sweeping all accumulated fees.
    """
    from app.services import daya_service
    from app.models.fee_sweep import SiiqoFeeSweep

    admin_id = get_jwt_identity()
    admin = _require_superadmin(_parse_admin_id(admin_id))
    if not admin:
        return jsonify({"message": "SuperAdmin required"}), 403

    corp_bank_code = os.environ.get("SIIQO_CORP_BANK_CODE", "")
    corp_account_number = os.environ.get("SIIQO_CORP_ACCOUNT_NUMBER", "")
    corp_account_name = os.environ.get("SIIQO_CORP_ACCOUNT_NAME", "Siiqo Marketplace Ltd")

    if not corp_bank_code or not corp_account_number:
        return jsonify({
            "message": (
                "SIIQO_CORP_BANK_CODE and SIIQO_CORP_ACCOUNT_NUMBER env vars "
                "must be set before sweeping."
            )
        }), 400

    data = request.get_json() or {}
    override_amount = data.get("amount_ngn")

    try:
        if override_amount:
            sweep_ngn = float(override_amount)
        else:
            total_fees = float(
                db.session.query(func.sum(EscrowTransaction.fee_amount))
                .filter(EscrowTransaction.status == EscrowStatus.RELEASED)
                .scalar() or 0
            )
            total_swept = float(
                db.session.query(func.sum(SiiqoFeeSweep.amount_ngn))
                .filter(SiiqoFeeSweep.status == "SUCCESS")
                .scalar() or 0
            )
            sweep_ngn = max(total_fees - total_swept, 0.0)

        if sweep_ngn < 100:
            return jsonify({
                "message": f"Nothing significant to sweep (₦{sweep_ngn:.2f})"
            }), 400

        # Live FX rate
        try:
            rate_info = daya_service.get_rate(asset="USDT", side="SELL")
            fx_rate = float(rate_info.get("rate", 1500.0))
        except Exception:
            fx_rate = 1500.0

        required_usd = round((sweep_ngn / fx_rate) * 1.02, 4)

        # Ensure Daya withdrawal balance is funded
        try:
            balance = daya_service.get_merchant_balance()
            bal_data = balance.get("data", {})
            collection_usd = float(bal_data.get("collection_balance_usd", 0))
            withdrawal_usd = float(bal_data.get("withdrawal_balance_usd", 0))
            if withdrawal_usd < required_usd:
                to_move = min(round(required_usd - withdrawal_usd + 0.20, 4), collection_usd)
                if to_move > 0:
                    daya_service.transfer_collection_to_withdrawal(
                        amount_usd=to_move,
                        idempotency_key=f"manual-sweep-bal-{uuid.uuid4().hex[:8]}",
                    )
        except Exception as bal_exc:
            logging.warning("[ADMIN SWEEP] Balance move failed: %s — continuing.", bal_exc)

        # Create pending sweep record
        sweep_ref = f"SIIQO-MANUAL-SWEEP-{uuid.uuid4().hex[:8].upper()}"
        sweep_record = SiiqoFeeSweep(
            reference=sweep_ref,
            amount_ngn=sweep_ngn,
            amount_usd=required_usd,
            fx_rate=fx_rate,
            bank_code=corp_bank_code,
            account_number=corp_account_number,
            account_name=corp_account_name,
            status="PENDING",
        )
        db.session.add(sweep_record)
        db.session.commit()

        # Trigger Daya NGN transfer
        result = daya_service.transfer_ngn_to_vendor(
            amount_ngn=sweep_ngn,
            bank_code=corp_bank_code,
            account_number=corp_account_number,
            account_name=corp_account_name,
            reference=sweep_ref,
        )

        if result.get("success"):
            sweep_record.status = "SUCCESS"
            sweep_record.daya_transfer_id = result.get("transfer_id")
            sweep_record.completed_at = _utcnow()
            db.session.commit()

            db.session.add(AdminAuditLog(
                admin_id=admin.id,
                admin_email=admin.email,
                admin_role=admin.role,
                action="DAYA_FEE_SWEEP",
                resource_type="SiiqoFeeSweep",
                resource_id=sweep_ref,
                details=f"Manual sweep ₦{sweep_ngn:,.2f} to account {corp_account_number} ref={sweep_ref}",
                ip_address=request.remote_addr or "0.0.0.0",
            ))
            db.session.commit()

            logging.info(
                "[ADMIN SWEEP] SUCCESS ₦%.2f ref=%s transfer_id=%s admin=%s",
                sweep_ngn, sweep_ref, result.get("transfer_id"), admin.email,
            )
            return jsonify({
                "status": "success",
                "message": f"₦{sweep_ngn:,.2f} swept to Siiqo corporate account.",
                "data": sweep_record.to_dict(),
            }), 200

        else:
            sweep_record.status = "FAILED"
            sweep_record.error_message = result.get("error_message", "Unknown error")
            db.session.commit()
            return jsonify({
                "status": "failed",
                "message": f"Daya transfer failed: {sweep_record.error_message}",
                "data": sweep_record.to_dict(),
            }), 502

    except Exception as exc:
        db.session.rollback()
        logging.error("[ADMIN SWEEP] %s", exc, exc_info=True)
        return jsonify({"message": f"Sweep failed: {exc}"}), 500


# ---------------------------------------------------------------------------
# GET /api/admin/daya/sweeps
# ---------------------------------------------------------------------------

@daya_admin_bp.route("/sweeps", methods=["GET"])
@jwt_required()
def list_fee_sweeps():
    """
    Lists all platform fee sweep records, newest first.
    Query params: ?page=1&per_page=20&status=SUCCESS
    """
    admin_id = get_jwt_identity()
    if not _require_superadmin(_parse_admin_id(admin_id)):
        return jsonify({"message": "SuperAdmin required"}), 403

    from app.models.fee_sweep import SiiqoFeeSweep

    page = int(request.args.get("page", 1))
    per_page = min(int(request.args.get("per_page", 20)), 100)
    status_filter = request.args.get("status")

    query = SiiqoFeeSweep.query
    if status_filter:
        query = query.filter_by(status=status_filter.upper())

    paginated = query.order_by(SiiqoFeeSweep.created_at.desc()).paginate(
        page=page, per_page=per_page, error_out=False
    )

    return jsonify({
        "status": "success",
        "data": [s.to_dict() for s in paginated.items],
        "pagination": {
            "page": page,
            "per_page": per_page,
            "total": paginated.total,
            "pages": paginated.pages,
        },
    }), 200
