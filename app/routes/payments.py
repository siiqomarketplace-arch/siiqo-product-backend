# payments.py - Daya crypto payment routes + vendor crypto wallet management
#
# Routes registered at /api/payments/* via payments_bp:
#   GET  /payments/vendor/crypto-wallet        -> get current wallet settings
#   POST /payments/vendor/crypto-wallet        -> save / update wallet settings
#   POST /payments/daya/initiate               -> create Daya funding account
#   GET  /payments/daya/status?order_id=X      -> poll deposit status
#   POST /payments/daya/refresh-rate           -> refresh expired rate
#   POST /payments/daya/webhook                -> deposit lifecycle events (no JWT)
#   POST /payments/initiate-pro-subscription   -> delegates to bridge.py

import hashlib
import hmac
import logging
import os
import re
import uuid
from datetime import datetime, timezone

from flask import Blueprint, request, jsonify
from flask_jwt_extended import jwt_required, get_jwt_identity

from app.extensions import db
from app.models.order import Order
from app.models.withdrawal import VendorCryptoWallet, DayaPayment
from app.services import daya_service

logger = logging.getLogger(__name__)
payments_bp = Blueprint("payments", __name__)


def _utcnow():
    return datetime.now(timezone.utc)


def _validate_wallet_address(address: str, network: str) -> tuple[bool, str]:
    """Returns (valid, reason). Reason is empty string when valid."""
    addr = address.strip()
    if not addr:
        return False, "Address is required"
    if network == "TRC20":
        if not re.match(r"^T[1-9A-HJ-NP-Za-km-z]{33}$", addr):
            return False, "TRC20 address must start with T and be 34 characters"
    elif network in ("ERC20", "BASE", "BEP20"):
        if not re.match(r"^0x[0-9a-fA-F]{40}$", addr):
            return False, "EVM address must start with 0x and be 42 characters"
    else:
        return False, f"Unknown network: {network}"
    return True, ""


# ===========================================================================
# VENDOR CRYPTO WALLET  - GET / POST /payments/vendor/crypto-wallet
# ===========================================================================

@payments_bp.route("/vendor/crypto-wallet", methods=["GET"])
@jwt_required()
def get_vendor_crypto_wallet():
    """Return the current vendor crypto wallet settings."""
    vendor_id = int(get_jwt_identity())
    wallet = VendorCryptoWallet.query.filter_by(vendor_id=vendor_id).first()
    if not wallet:
        return jsonify({
            "wallet_address": "",
            "asset": "USDT",
            "network": "TRC20",
            "accepts_crypto": False,
        }), 200
    return jsonify(wallet.to_dict()), 200


@payments_bp.route("/vendor/crypto-wallet", methods=["POST"])
@jwt_required()
def save_vendor_crypto_wallet():
    """Create or update the vendor's crypto wallet settings."""
    vendor_id = int(get_jwt_identity())
    data = request.get_json() or {}

    wallet_address = (data.get("wallet_address") or "").strip()
    asset          = data.get("asset", "USDT")
    network        = data.get("network", "TRC20")
    accepts_crypto = bool(data.get("accepts_crypto", False))

    if asset not in ("USDT", "USDC"):
        return jsonify({"message": "asset must be USDT or USDC"}), 400
    if network not in ("TRC20", "ERC20", "BASE", "BEP20"):
        return jsonify({"message": "network must be TRC20, ERC20, BASE, or BEP20"}), 400

    if accepts_crypto:
        if not wallet_address:
            return jsonify({"message": "wallet_address is required when accepts_crypto is true"}), 400
        valid, reason = _validate_wallet_address(wallet_address, network)
        if not valid:
            return jsonify({"message": reason}), 400

    wallet = VendorCryptoWallet.query.filter_by(vendor_id=vendor_id).first()
    if wallet:
        wallet.wallet_address = wallet_address
        wallet.asset          = asset
        wallet.network        = network
        wallet.accepts_crypto = accepts_crypto
        wallet.updated_at     = _utcnow()
    else:
        wallet = VendorCryptoWallet(
            vendor_id      = vendor_id,
            wallet_address = wallet_address,
            asset          = asset,
            network        = network,
            accepts_crypto = accepts_crypto,
        )
        db.session.add(wallet)

    db.session.commit()
    return jsonify({"message": "Crypto wallet settings saved", "data": wallet.to_dict()}), 200


# ===========================================================================
# POST /payments/daya/initiate
# ===========================================================================

@payments_bp.route("/daya/initiate", methods=["POST"])
@jwt_required()
def daya_initiate():
    """Create a Daya funding account for a crypto payment."""
    buyer_user_id = int(get_jwt_identity())
    data = request.get_json() or {}

    order_id_param = data.get("orderId") or data.get("order_id", "")
    primary_order_id = int(str(order_id_param).split(",")[0]) if order_id_param else None
    if not primary_order_id:
        return jsonify({"message": "orderId is required"}), 400

    amount_ngn_raw = data.get("amountNgn") or data.get("amount_ngn", 0)
    try:
        amount_ngn = float(amount_ngn_raw)
    except (TypeError, ValueError):
        return jsonify({"message": "amountNgn must be a number"}), 400

    if amount_ngn <= 0:
        return jsonify({"message": "amountNgn must be greater than 0"}), 400

    buyer_email  = (data.get("buyerEmail") or data.get("buyer_email") or "").strip()
    buyer_name   = (data.get("buyerName")  or data.get("buyer_name")  or "").strip()
    payment_type = data.get("type", "ngn_onramp")
    asset        = data.get("asset", "USDT")
    network      = data.get("network", "TRC20")

    if payment_type not in ("ngn_onramp", "crypto_direct"):
        return jsonify({"message": "type must be ngn_onramp or crypto_direct"}), 400
    if asset not in ("USDT", "USDC"):
        return jsonify({"message": "asset must be USDT or USDC"}), 400
    if not _key_configured():
        return jsonify({"message": "Crypto payments are not configured yet"}), 503

    order = db.session.get(Order, primary_order_id)
    if not order:
        return jsonify({"message": "Order not found"}), 404
    if order.buyer_id != buyer_user_id:
        return jsonify({"message": "Unauthorized"}), 403

    existing = DayaPayment.query.filter_by(
        order_id=primary_order_id,
        payment_type=payment_type,
    ).filter(DayaPayment.status.in_(["PENDING", "RECEIVED", "REQUIRES_REVIEW"])).first()

    if existing and existing.daya_funding_account_id:
        if existing.rate_expires_at and existing.rate_expires_at > _utcnow():
            return _build_initiate_response(existing), 200

    try:
        name_parts = buyer_name.split(" ", 1)
        first = name_parts[0] if name_parts else buyer_email.split("@")[0]
        last  = name_parts[1] if len(name_parts) > 1 else ""
        daya_customer_id = daya_service.get_or_create_customer(buyer_email, first, last)
    except RuntimeError as exc:
        logger.error("[DAYA INITIATE] Customer creation failed: %s", exc)
        return jsonify({"message": f"Could not create payment session: {exc}"}), 502

    try:
        side = "BUY" if payment_type == "ngn_onramp" else "SELL"
        rate_data = daya_service.get_rate(asset=asset, side=side)
    except RuntimeError as exc:
        logger.error("[DAYA INITIATE] Rate fetch failed: %s", exc)
        return jsonify({"message": f"Could not fetch exchange rate: {exc}"}), 502

    rate_id    = rate_data["rate_id"]
    rate       = float(rate_data["rate"])
    expires_at = rate_data["expires_at"]
    import time
    idem_key   = f"siiqo-{primary_order_id}-{payment_type}-{rate_id}-{int(time.time())}"

    try:
        if payment_type == "ngn_onramp":
            try:
                fa = daya_service.create_ngn_funding_account(
                    customer_id=daya_customer_id,
                    amount_ngn=int(round(amount_ngn)),
                    rate_id=rate_id,
                    idempotency_key=idem_key,
                    developer_fee_pct="0",
                )
            except RuntimeError as first_exc:
                logger.warning("[DAYA INITIATE] First attempt for virtual account failed (%s), retrying with fresh key...", first_exc)
                time.sleep(0.5)
                idem_key_retry = f"siiqo-{primary_order_id}-{payment_type}-{rate_id}-{int(time.time())}-r2"
                fa = daya_service.create_ngn_funding_account(
                    customer_id=daya_customer_id,
                    amount_ngn=int(round(amount_ngn)),
                    rate_id=rate_id,
                    idempotency_key=idem_key_retry,
                    developer_fee_pct="0",
                )
            instructions = fa.get("instructions", [{}])[0]
            bank_name      = instructions.get("bank_name", "")
            account_number = instructions.get("account_number", "")
            account_name   = instructions.get("account_name", "")
            wallet_addr    = None
            amount_crypto  = None
            # Use the amount Daya returns — this is what the buyer MUST send
            # (includes Daya's processing fee, may differ from the order total)
            daya_amount_ngn = fa.get("amount", amount_ngn)
            amount_ngn = float(daya_amount_ngn)
        else:
            crypto_amount = round(amount_ngn / rate, 6)
            amount_crypto = f"{crypto_amount:.6f}".rstrip("0").rstrip(".")
            try:
                fa = daya_service.create_crypto_funding_account(
                    customer_id=daya_customer_id,
                    asset=asset,
                    network=network,
                    rate_id=rate_id,
                    idempotency_key=idem_key,
                    developer_fee_pct="0",
                )
            except RuntimeError as first_exc:
                logger.warning("[DAYA INITIATE] First attempt for crypto account failed (%s), retrying with fresh key...", first_exc)
                time.sleep(0.5)
                idem_key_retry = f"siiqo-{primary_order_id}-{payment_type}-{rate_id}-{int(time.time())}-r2"
                fa = daya_service.create_crypto_funding_account(
                    customer_id=daya_customer_id,
                    asset=asset,
                    network=network,
                    rate_id=rate_id,
                    idempotency_key=idem_key_retry,
                    developer_fee_pct="0",
                )
            instructions = fa.get("instructions", [{}])[0]
            wallet_addr    = instructions.get("address", "")
            bank_name      = None
            account_number = None
            account_name   = None
    except RuntimeError as exc:
        logger.error("[DAYA INITIATE] Funding account creation failed: %s", exc)
        return jsonify({"message": f"Could not create payment address: {exc}"}), 502

    fa_id = fa["id"]
    from datetime import datetime as _dt
    expires_dt = _dt.fromisoformat(expires_at.replace("Z", "+00:00")) if expires_at else None

    dp = DayaPayment.query.filter_by(order_id=primary_order_id).first()
    if dp:
        dp.payment_type             = payment_type
        dp.daya_funding_account_id  = fa_id
        dp.daya_rate_id             = rate_id
        dp.rate_expires_at          = expires_dt
        dp.amount_ngn               = amount_ngn
        dp.amount_crypto            = amount_crypto
        dp.asset                    = asset
        dp.network                  = network
        dp.bank_name                = bank_name
        dp.account_number           = account_number
        dp.account_name             = account_name
        dp.wallet_address           = wallet_addr
        dp.status                   = "PENDING"
        dp.rate                     = rate
        dp.updated_at               = _utcnow()
        # Reset created_at so the stale deposit guard uses THIS session's start time,
        # not the timestamp of a previous payment attempt on the same order.
        dp.created_at               = _utcnow()
    else:
        dp = DayaPayment(
            order_id=primary_order_id,
            buyer_id=buyer_user_id,
            payment_type=payment_type,
            daya_funding_account_id=fa_id,
            daya_rate_id=rate_id,
            rate_expires_at=expires_dt,
            amount_ngn=amount_ngn,
            amount_crypto=amount_crypto,
            asset=asset,
            network=network,
            bank_name=bank_name,
            account_number=account_number,
            account_name=account_name,
            wallet_address=wallet_addr,
            status="PENDING",
            rate=rate,
        )
        db.session.add(dp)

    db.session.commit()
    return _build_initiate_response(dp), 200


def _key_configured() -> bool:
    return bool(os.environ.get("DAYA_API_KEY", ""))


def _build_initiate_response(dp: DayaPayment):
    """Serialise a DayaPayment into the JSON shape the frontend expects."""
    payload = {
        "type":             dp.payment_type,
        "fundingAccountId": dp.daya_funding_account_id,
        "rateId":           dp.daya_rate_id,
        "expiresAt":        dp.rate_expires_at.isoformat() if dp.rate_expires_at else None,
        "amountNgn":        float(dp.amount_ngn),
        "orderId":          str(dp.order_id),
    }
    if dp.payment_type == "ngn_onramp":
        payload.update({
            "bankName":      dp.bank_name,
            "accountNumber": dp.account_number,
            "accountName":   dp.account_name,
            "rate":          float(dp.rate) if dp.rate else 0,
        })
    else:
        payload.update({
            "walletAddress": dp.wallet_address,
            "amountCrypto":  dp.amount_crypto,
            "asset":         dp.asset,
            "network":       dp.network,
            "rate":          float(dp.rate) if dp.rate else 0,
        })
    return jsonify(payload)


# ===========================================================================
# GET /payments/daya/status?order_id=X
# ===========================================================================

@payments_bp.route("/daya/status", methods=["GET"])
@jwt_required(optional=True)
def daya_status():
    """Poll the Daya deposit status for an order.
    Works with or without authentication — Pay Link buyers are guests (no token).
    When authenticated, validates buyer ownership. When guest, uses order_id only.
    """
    buyer_user_id = get_jwt_identity()
    order_id_str  = request.args.get("order_id", "")

    try:
        order_id = int(order_id_str.split(",")[0])
    except (ValueError, IndexError):
        return jsonify({"message": "order_id is required"}), 400

    dp = DayaPayment.query.filter_by(order_id=order_id).first()
    if not dp:
        return jsonify({"message": "No crypto payment found for this order"}), 404

    # Only enforce ownership check when buyer is authenticated
    if buyer_user_id and dp.buyer_id != int(buyer_user_id):
        return jsonify({"message": "Unauthorized"}), 403

    if dp.status in ("COMPLETED", "FAILED"):
        return jsonify({
            "orderId": str(order_id),
            "status":  dp.status,
            "paidAt":  dp.updated_at.isoformat() if dp.status == "COMPLETED" else None,
        }), 200

    if dp.rate_expires_at and dp.rate_expires_at < _utcnow() and dp.status == "PENDING":
        dp.status     = "EXPIRED"
        dp.updated_at = _utcnow()
        db.session.commit()
        return jsonify({"orderId": str(order_id), "status": "EXPIRED"}), 200

    if dp.daya_funding_account_id and _key_configured():
        try:
            deposit = daya_service.get_deposit_by_funding_account(dp.daya_funding_account_id)
            if deposit:
                # ── STALE DEPOSIT GUARD ────────────────────────────────────────
                # Crypto funding accounts are PERMANENT (reused across orders).
                # get_deposit_by_funding_account returns limit=1 (most recent deposit).
                # If a previous order already completed on this address, that old
                # COMPLETED deposit would be returned here and falsely trigger
                # order confirmation BEFORE the buyer has transferred anything.
                #
                # We only accept this deposit if it was created AFTER this
                # DayaPayment record was created (i.e. it belongs to THIS order).
                deposit_created_raw = deposit.get("created_at") or deposit.get("createdAt", "")
                deposit_is_fresh = False
                if deposit_created_raw and dp.created_at:
                    try:
                        from datetime import datetime as _dt2
                        dep_ts = _dt2.fromisoformat(
                            deposit_created_raw.replace("Z", "+00:00")
                        )
                        # Give 30-second grace period for clock skew
                        from datetime import timedelta
                        cutoff = dp.created_at.replace(tzinfo=timezone.utc) - timedelta(seconds=30)
                        deposit_is_fresh = dep_ts >= cutoff
                    except Exception:
                        deposit_is_fresh = False  # If we can't parse, don't trust it
                else:
                    # No timestamp available — can't verify, treat as stale
                    deposit_is_fresh = False

                if not deposit_is_fresh:
                    logger.info(
                        "[DAYA STATUS] Order %s — deposit %s predates this payment session "
                        "(deposit created_at=%s, payment created_at=%s). Ignoring stale deposit.",
                        order_id, deposit.get("id"), deposit_created_raw,
                        dp.created_at.isoformat() if dp.created_at else "unknown",
                    )
                else:
                    new_status = daya_service._map_daya_status(deposit.get("status", ""))
                    dp.daya_deposit_id = deposit.get("id")
                    if new_status != dp.status:
                        dp.status     = new_status
                        dp.updated_at = _utcnow()
                        db.session.commit()
                        if new_status == "COMPLETED":
                            _handle_crypto_payment_confirmed(order_id, dp)
        except Exception as exc:
            logger.warning("[DAYA STATUS] Poll failed for order %s: %s", order_id, exc)

    return jsonify({
        "orderId": str(order_id),
        "status":  dp.status,
        "paidAt":  dp.updated_at.isoformat() if dp.status == "COMPLETED" else None,
    }), 200


# ===========================================================================
# POST /payments/daya/refresh-rate
# ===========================================================================

@payments_bp.route("/daya/refresh-rate", methods=["POST"])
@jwt_required()
def daya_refresh_rate():
    """Refresh an expired Daya rate for an existing pending payment."""
    buyer_user_id = int(get_jwt_identity())
    data = request.get_json() or {}

    order_id_str = str(data.get("orderId") or data.get("order_id", ""))
    try:
        order_id = int(order_id_str.split(",")[0])
    except (ValueError, IndexError):
        return jsonify({"message": "orderId is required"}), 400

    dp = DayaPayment.query.filter_by(order_id=order_id).first()
    if not dp:
        return jsonify({"message": "No crypto payment found for this order"}), 404
    if dp.buyer_id != buyer_user_id:
        return jsonify({"message": "Unauthorized"}), 403
    if not _key_configured():
        return jsonify({"message": "Crypto payments not configured"}), 503

    try:
        side = "BUY" if dp.payment_type == "ngn_onramp" else "SELL"
        rate_data = daya_service.get_rate(asset=dp.asset or "USDT", side=side)
    except RuntimeError as exc:
        return jsonify({"message": f"Could not fetch new rate: {exc}"}), 502

    rate_id    = rate_data["rate_id"]
    rate       = float(rate_data["rate"])
    expires_at = rate_data["expires_at"]

    from datetime import datetime as _dt
    expires_dt = _dt.fromisoformat(expires_at.replace("Z", "+00:00")) if expires_at else None

    new_amount_crypto = None
    if dp.payment_type == "crypto_direct" and dp.amount_ngn:
        crypto_val = round(float(dp.amount_ngn) / rate, 6)
        new_amount_crypto = f"{crypto_val:.6f}".rstrip("0").rstrip(".")
        dp.amount_crypto  = new_amount_crypto

    dp.daya_rate_id    = rate_id
    dp.rate_expires_at = expires_dt
    dp.rate            = rate
    dp.status          = "PENDING"
    dp.updated_at      = _utcnow()
    db.session.commit()

    response = {"rate": rate, "rateId": rate_id, "expiresAt": expires_at}
    if new_amount_crypto:
        response["amountCrypto"] = new_amount_crypto
    else:
        response["amountNgn"] = float(dp.amount_ngn)

    return jsonify(response), 200


# ===========================================================================
# POST /payments/daya/webhook  - no JWT, HMAC verified
# ===========================================================================

@payments_bp.route("/daya/webhook", methods=["POST"])
def daya_webhook():
    """Receive Daya deposit lifecycle events."""
    payload_bytes = request.get_data()
    sig_header    = request.headers.get("X-Daya-Signature", "")

    if not daya_service.verify_webhook_signature(payload_bytes, sig_header):
        logger.warning("[DAYA WEBHOOK] Invalid signature")
        return jsonify({"message": "Invalid signature"}), 401

    try:
        event_data = request.get_json(force=True) or {}
    except Exception:
        return jsonify({"message": "Invalid JSON"}), 400

    event_type = event_data.get("event", "")
    deposit    = event_data.get("data", {})
    logger.info("[DAYA WEBHOOK] event=%s deposit_id=%s status=%s",
                event_type, deposit.get("id"), deposit.get("status"))

    if event_type == "deposit.completed":
        funding_account_id = deposit.get("funding_account_id")
        deposit_id         = deposit.get("id")
        deposit_created_raw = deposit.get("created_at") or deposit.get("createdAt", "")

        if funding_account_id:
            dp = DayaPayment.query.filter_by(
                daya_funding_account_id=funding_account_id
            ).filter(DayaPayment.status.notin_(["COMPLETED", "FAILED"])).first()

            if dp:
                # ── STALE DEPOSIT GUARD (webhook) ─────────────────────────────
                # Permanent funding accounts are reused. Verify this deposit was
                # created AFTER this payment session was initiated.
                deposit_is_fresh = False
                if deposit_created_raw and dp.created_at:
                    try:
                        from datetime import datetime as _dt2, timedelta
                        dep_ts = _dt2.fromisoformat(
                            deposit_created_raw.replace("Z", "+00:00")
                        )
                        cutoff = dp.created_at.replace(tzinfo=timezone.utc) - timedelta(seconds=30)
                        deposit_is_fresh = dep_ts >= cutoff
                    except Exception:
                        # Can't parse timestamp — trust the webhook but log it
                        logger.warning(
                            "[DAYA WEBHOOK] Could not parse deposit created_at=%s for order %s. "
                            "Proceeding cautiously.",
                            deposit_created_raw, dp.order_id
                        )
                        deposit_is_fresh = True  # Webhooks come from Daya directly — trust them
                else:
                    # No timestamp on the deposit — webhook is direct from Daya, trust it
                    deposit_is_fresh = True

                if deposit_is_fresh:
                    dp.status          = "COMPLETED"
                    dp.daya_deposit_id = deposit_id
                    dp.updated_at      = _utcnow()
                    db.session.commit()
                    _handle_crypto_payment_confirmed(dp.order_id, dp)
                else:
                    logger.warning(
                        "[DAYA WEBHOOK] Stale deposit %s (created_at=%s) arrived for "
                        "order %s (payment_created_at=%s). Ignoring.",
                        deposit_id, deposit_created_raw,
                        dp.order_id,
                        dp.created_at.isoformat() if dp.created_at else "unknown",
                    )

    return jsonify({"received": True}), 200


# ===========================================================================
# Internal helper
# ===========================================================================

def _handle_crypto_payment_confirmed(order_id: int, dp: DayaPayment):
    """Trigger order confirmation for a completed crypto payment."""
    from app.models.escrow import EscrowTransaction, EscrowStatus
    from app.models.communication import Notification

    try:
        order = db.session.get(Order, order_id)
        if not order:
            logger.error("[DAYA CONFIRM] Order %s not found", order_id)
            return
        if order.status in ("PAID", "COMPLETED", "RELEASED"):
            logger.info("[DAYA CONFIRM] Order %s already confirmed -- skipping", order_id)
            return

        order.status = "PAID"
        if dp and dp.payment_type in ("bank_transfer", "ngn_onramp"):
            order.payment_method = "DAYA_BANK_TRANSFER"
        else:
            order.payment_method = "CRYPTO"

        escrow = EscrowTransaction.query.filter_by(order_id=order_id).first()
        if not escrow:
            fee_rate = 0.054 if (order.vendor and order.vendor.storefront and order.vendor.storefront.is_pro_verified) else 0.06
            fee_amount = round(float(order.total_amount) * fee_rate, 2)
            txn_number = f"DYA-{uuid.uuid4().hex[:12].upper()}"
            escrow = EscrowTransaction(
                order_id=order_id,
                transaction_number=txn_number,
                status=EscrowStatus.IN_ESCROW,
                amount=float(order.total_amount),
                fee_percent=fee_rate * 100,
                fee_amount=fee_amount,
                payment_link=None,
                payscrow_transaction_id=f"DAYA-{dp.daya_deposit_id or dp.daya_funding_account_id}",
            )
            escrow.paid_at = _utcnow()
            db.session.add(escrow)
        else:
            escrow.status  = EscrowStatus.IN_ESCROW
            escrow.paid_at = escrow.paid_at or _utcnow()
            if dp.daya_deposit_id:
                escrow.payscrow_transaction_id = f"DAYA-{dp.daya_deposit_id}"

        db.session.flush()

        # ── Event Ticket Orders: activate tickets and credit revenue ───────────
        from app.routes.events import activate_tickets_for_order
        is_ticket_order = activate_tickets_for_order(order_id)
        if is_ticket_order:
            from app.routes.escrow import _credit_vendor_ledger
            net_amount = float(escrow.amount) - float(escrow.fee_amount or 0)
            escrow.status = EscrowStatus.RELEASED
            escrow.released_at = _utcnow()
            order.status = 'COMPLETED'
            _credit_vendor_ledger(
                vendor_id=order.vendor_id,
                amount=net_amount,
                reference_id=escrow.transaction_number,
                description=f"Auto-released payout for Event Order #{order.id}",
            )
            _payout_vendor_via_daya(order, escrow)

        from app.routes.escrow import _deliver_digital_products, _deliver_service_products
        is_digital = _deliver_digital_products(order, escrow)
        is_service = False
        if not is_digital:
            is_service = _deliver_service_products(order, escrow)

        # ── Pay Link orders: product_id=None so _deliver_* return False.
        # Detect product type from the linked PaymentLink instead.
        if not is_digital and not is_service and order.payment_link_id:
            from app.models.payment_link import PaymentLink as _PL
            _link = db.session.get(_PL, order.payment_link_id)
            _ltype = getattr(_link, 'product_type', 'physical') or 'physical'
            if _ltype == 'digital':
                # Digital Pay Link — release immediately, credit vendor
                fee_rate = 0.054 if (order.vendor and order.vendor.storefront and order.vendor.storefront.is_pro_verified) else 0.06
                net_amount = float(escrow.amount) - float(escrow.fee_amount or 0)
                escrow.status = EscrowStatus.RELEASED
                escrow.released_at = _utcnow()
                order.status = 'COMPLETED'
                if _link: _link.status = 'PAID'
                _payout_vendor_via_daya(order, escrow)
                db.session.add(Notification(
                    user_id=order.buyer_id,
                    title="Payment Complete",
                    message=f"Your payment for Order #{order_id} is confirmed. The vendor has been notified.",
                    type="ORDER", order_id=order_id,
                ))
                db.session.add(Notification(
                    user_id=order.vendor_id,
                    title="Digital Sale Complete",
                    message=f"Order #{order_id} paid. ₦{net_amount:,.2f} credited.",
                    type="ESCROW", order_id=order_id,
                ))
                is_digital = True
            elif _ltype == 'service':
                # Service Pay Link — release immediately
                net_amount = float(escrow.amount) - float(escrow.fee_amount or 0)
                escrow.status = EscrowStatus.RELEASED
                escrow.released_at = _utcnow()
                order.status = 'COMPLETED'
                if _link: _link.status = 'PAID'
                _payout_vendor_via_daya(order, escrow)
                db.session.add(Notification(
                    user_id=order.buyer_id,
                    title="Payment Complete",
                    message=f"Your payment for Order #{order_id} is confirmed.",
                    type="ORDER", order_id=order_id,
                ))
                db.session.add(Notification(
                    user_id=order.vendor_id,
                    title="Service Sale Complete",
                    message=f"Order #{order_id} paid. ₦{net_amount:,.2f} credited.",
                    type="ESCROW", order_id=order_id,
                ))
                is_service = True
            # else: physical Pay Link — falls through to hold-in-escrow logic below
            # Mark INVOICE as PAID regardless of product type once payment confirmed
            if _link and _link.link_type == 'INVOICE' and not is_digital and not is_service:
                _link.status = 'PAID'

        # ── Vendor payout via Daya for digital/service orders ─────────────
        # For physical orders, payout happens when buyer confirms delivery.
        # For digital/service, payout is immediate (same as Paystack flow).
        if is_digital or is_service:
            _payout_vendor_via_daya(order, escrow)

        if not is_digital and not is_service:
            # Physical order — create or activate the LogisticsAssignment
            from app.models.escrow import LogisticsAssignment
            assignment = LogisticsAssignment.query.filter_by(order_id=order_id).first()
            if assignment:
                # Assignment was pre-created at checkout — activate it now
                if assignment.status == 'PENDING':
                    assignment.status = 'ASSIGNED'
                    assignment.assigned_at = _utcnow()
                    db.session.add(Notification(
                        user_id=assignment.partner_id,
                        title="New Delivery Assignment",
                        message=(
                            f"New delivery for Order #{order_id}. "
                            f"Fee: ₦{float(assignment.delivery_fee):,.2f}."
                        ),
                        type="DELIVERY",
                        order_id=order_id,
                    ))
            is_naira = dp and dp.payment_type in ("bank_transfer", "ngn_onramp")
            notif_title = "Payment Received (Bank Transfer)" if is_naira else "Crypto Payment Received"
            notif_msg = (
                f"Naira bank transfer confirmed for Order #{order_id}. Please ship the order."
                if is_naira
                else f"Crypto payment confirmed for Order #{order_id}. Please ship the order."
            )
            buyer_notif_msg = (
                f"Your bank transfer payment for Order #{order_id} has been confirmed."
                if is_naira
                else f"Your crypto payment for Order #{order_id} has been confirmed."
            )
            db.session.add(Notification(
                user_id=order.vendor_id,
                title=notif_title,
                message=notif_msg,
                type="ESCROW",
                order_id=order_id,
            ))
            db.session.add(Notification(
                user_id=order.buyer_id,
                title="Payment Confirmed",
                message=buyer_notif_msg,
                type="ORDER",
                order_id=order_id,
            ))

        db.session.commit()
        logger.info("[DAYA CONFIRM] Order %s confirmed (digital=%s service=%s)",
                    order_id, is_digital, is_service)

    except Exception as exc:
        db.session.rollback()
        logger.error("[DAYA CONFIRM] Error confirming Order %s: %s", order_id, exc)


def _payout_vendor_via_daya(order, escrow):
    """
    Pay vendor their 94% share after a crypto order is confirmed.

    Flow A — buyer paid NGN onramp:
      → Move collection USD → withdrawal, then Daya NGN bank transfer to vendor bank.

    Flow B — buyer paid USDT/USDC direct:
      → Move collection USD → withdrawal, then Daya on-chain withdrawal to vendor wallet.
      → Falls back to Flow A (NGN bank) if vendor has no crypto wallet configured.

    NOTE: Paystack is NOT used here. Crypto order funds always land in Daya's collection
    balance. Paystack has no balance to send from after a crypto payment.
    """
    from app.models.withdrawal import VendorBankAccount, VendorCryptoWallet, DayaPayment
    from app.models.communication import Notification

    net_amount_ngn = float(escrow.amount) - float(escrow.fee_amount or 0)

    dp = DayaPayment.query.filter_by(order_id=order.id).first()
    payment_type = dp.payment_type if dp else "ngn_onramp"

    # ── Step 1: Move collection → withdrawal so funds are available to send ──────
    # Fetch live rate dynamically from Daya (with fallback to locked payment rate or 1500)
    current_rate = float(dp.rate) if (dp and dp.rate and float(dp.rate) > 0) else 1500.0
    try:
        rate_data = daya_service.get_rate(asset=dp.asset if dp else "USDT", side="SELL")
        if rate_data and rate_data.get("rate"):
            current_rate = float(rate_data["rate"])
    except Exception as _rate_exc:
        logger.info("[DAYA PAYOUT] Rate fetch fallback for Order %s: using rate %.2f (%s)", order.id, current_rate, _rate_exc)

    try:
        balance = daya_service.get_merchant_balance()
        bal_data = balance.get("data", {})
        collection_usd = float(bal_data.get("collection_balance_usd", 0))
        withdrawal_usd = float(bal_data.get("withdrawal_balance_usd", 0))
        logger.info(
            "[DAYA PAYOUT] Order %s balances — collection: $%.4f  withdrawal: $%.4f (rate: %.2f)",
            order.id, collection_usd, withdrawal_usd, current_rate
        )
        # USD estimate: NGN amount / current_rate + 2% buffer for fees/spread
        estimated_usd_needed = round((net_amount_ngn / current_rate) * 1.02, 4)
        if withdrawal_usd < estimated_usd_needed:
            shortfall = estimated_usd_needed - withdrawal_usd
            amount_to_move = min(round(shortfall + 0.10, 4), collection_usd)
            if amount_to_move > 0:
                transfer_idem = f"bal-transfer-{order.id}-{uuid.uuid4().hex[:8]}"
                daya_service.transfer_collection_to_withdrawal(
                    amount_usd=amount_to_move,
                    idempotency_key=transfer_idem,
                )
                logger.info(
                    "[DAYA PAYOUT] Order %s -- moved $%.4f collection→withdrawal",
                    order.id, amount_to_move
                )
    except Exception as exc:
        logger.warning(
            "[DAYA PAYOUT] Order %s -- balance check/move failed: %s. "
            "Attempting payout anyway (withdrawal balance may already be sufficient).",
            order.id, exc
        )

    payout_ref = f"CRYPTO-PAYOUT-{order.id}-{uuid.uuid4().hex[:8].upper()}"

    # ── Flow B: buyer paid crypto directly — pay vendor on-chain ─────────────────
    if payment_type == "crypto_direct":
        crypto_wallet = VendorCryptoWallet.query.filter_by(
            vendor_id=order.vendor_id, accepts_crypto=True
        ).first()

        if not crypto_wallet or not crypto_wallet.wallet_address:
            logger.warning(
                "[DAYA PAYOUT] Order %s -- vendor %s has no crypto wallet configured. "
                "Falling back to NGN bank payout.",
                order.id, order.vendor_id
            )
            # Fall through to Flow A
        else:
            # Convert net NGN → USD using current rate
            net_usd = round(net_amount_ngn / current_rate, 6)
            chain = daya_service.NETWORK_TO_DAYA_CHAIN.get(
                crypto_wallet.network, crypto_wallet.network
            )
            try:
                result = daya_service.withdraw_usdt_to_wallet(
                    amount_usd=net_usd,
                    token=crypto_wallet.asset,
                    chain=chain,
                    destination_address=crypto_wallet.wallet_address,
                    idempotency_key=payout_ref,
                )
                logger.info(
                    "[DAYA PAYOUT] Order %s -- on-chain %s sent: $%.6f → %s chain=%s",
                    order.id, crypto_wallet.asset, net_usd,
                    crypto_wallet.wallet_address, chain
                )
                db.session.add(Notification(
                    user_id=order.vendor_id,
                    title="Payment Sent to Your Wallet",
                    message=(
                        f"Order #{order.id} is complete. "
                        f"{crypto_wallet.asset} is on its way to your {crypto_wallet.network} wallet."
                    ),
                    type="ESCROW",
                    order_id=order.id,
                ))
                return
            except RuntimeError as exc:
                logger.error(
                    "[DAYA PAYOUT] Order %s -- on-chain withdrawal failed: %s. "
                    "Falling back to NGN bank transfer.",
                    order.id, exc
                )
                # Fall through to Flow A

    # ── Flow A: NGN bank transfer to vendor's registered bank account ─────────────
    bank_acc = VendorBankAccount.query.filter_by(
        vendor_id=order.vendor_id, is_default=True
    ).first() or VendorBankAccount.query.filter_by(
        vendor_id=order.vendor_id
    ).first()

    bank_code = bank_acc.bank_code if bank_acc else None
    account_number = bank_acc.account_number if bank_acc else None
    account_name = (bank_acc.account_name if bank_acc else "") or ""

    if not bank_code or not account_number:
        from app.models.user import Storefront
        sf = Storefront.query.filter_by(vendor_id=order.vendor_id).first()
        if sf and sf.bank_code and sf.account_number:
            bank_code = sf.bank_code
            account_number = sf.account_number
            account_name = sf.business_name or (order.vendor.full_name if order.vendor else "")

    if not bank_code or not account_number:
        logger.error(
            "[DAYA PAYOUT] Order %s -- vendor %s has no bank account registered. "
            "Cannot auto-payout. NGN%.2f is still in Daya withdrawal balance.",
            order.id, order.vendor_id, net_amount_ngn
        )
        db.session.add(Notification(
            user_id=order.vendor_id,
            title="Payout Pending — Add Bank Account",
            message=(
                f"Your order #{order.id} is complete and payment is confirmed. "
                "To receive your payout, please add your bank account in "
                "Settings → Payout Settings. Contact support@siiqo.com if you need help."
            ),
            type="ESCROW",
            order_id=order.id,
        ))
        return

    # Attempt the NGN transfer directly — no pre-resolve needed.
    # Daya validates the account internally on transfer.
    result = daya_service.transfer_ngn_to_vendor(
        amount_ngn=net_amount_ngn,
        bank_code=bank_code,
        account_number=account_number,
        account_name=account_name,
        reference=payout_ref,
        order_id=order.id,
    )

    if result.get("success"):
        logger.info(
            "[DAYA PAYOUT] Order %s -- NGN payout initiated: NGN%.2f → %s/%s ref=%s",
            order.id, net_amount_ngn, bank_code,
            account_number, payout_ref
        )
        db.session.add(Notification(
            user_id=order.vendor_id,
            title="Payment On Its Way",
            message=(
                f"Order #{order.id} is complete. "
                f"NGN{net_amount_ngn:,.2f} is being transferred to your bank account "
                f"({bank_acc.bank_name or bank_acc.bank_code} ···{bank_acc.account_number[-4:]})."
            ),
            type="ESCROW",
            order_id=order.id,
        ))
    else:
        # Daya NGN transfer failed — funds are still safely in Daya withdrawal balance.
        # Log clearly so support can manually retry. Do NOT attempt Paystack (no balance there).
        logger.error(
            "[DAYA PAYOUT] Order %s -- NGN transfer failed: %s. "
            "NGN%.2f remains in Daya withdrawal balance. Manual retry needed.",
            order.id, result.get("error_message"), net_amount_ngn
        )
        db.session.add(Notification(
            user_id=order.vendor_id,
            title="Payout Processing — Contact Support",
            message=(
                f"Your order #{order.id} is complete and payment is confirmed. "
                "Your payout is being processed — if you don't receive it within 2 hours, "
                "please contact support@siiqo.com with your order number."
            ),
            type="ESCROW",
            order_id=order.id,
        ))




# ===========================================================================
# POST /payments/initiate-pro-subscription
# ===========================================================================

@payments_bp.route("/initiate-pro-subscription", methods=["POST"])
@jwt_required()
def initiate_pro_subscription():
    """Delegates to the existing bridge.py handler."""
    from app.routes.bridge import initiate_pro_subscription as _bridge_sub
    return _bridge_sub()
