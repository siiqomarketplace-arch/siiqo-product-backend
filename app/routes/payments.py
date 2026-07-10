"""
"""
payments.py — Daya crypto payment routes + vendor crypto wallet management

Routes registered at /api/payments/* via payments_bp:

  Vendor wallet:
    GET  /payments/vendor/crypto-wallet        → get current wallet settings
    POST /payments/vendor/crypto-wallet        → save / update wallet settings

  Buyer crypto payment (Daya):
    POST /payments/daya/initiate               → create Daya funding account
    GET  /payments/daya/status?order_id=X      → poll deposit status
    POST /payments/daya/refresh-rate           → refresh expired rate

  Daya webhook (no auth — verified by HMAC):
    POST /payments/daya/webhook                → deposit lifecycle events

  Pro subscription (existing — kept here for completeness):
    POST /payments/initiate-pro-subscription   → delegates to bridge.py
"""

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


# ---------------------------------------------------------------------------
# Address format validation (mirrors frontend dayaService.validateWalletAddress)
# ---------------------------------------------------------------------------

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
# VENDOR CRYPTO WALLET  — GET / POST /payments/vendor/crypto-wallet
# Also served at /api/vendor/crypto-wallet via bridge aliases below
# ===========================================================================

@payments_bp.route("/vendor/crypto-wallet", methods=["GET"])
@jwt_required()
def get_vendor_crypto_wallet():
    """Return the current vendor crypto wallet settings."""
    vendor_id = int(get_jwt_identity())
    wallet = VendorCryptoWallet.query.filter_by(vendor_id=vendor_id).first()
    if not wallet:
        # Return empty defaults — vendor hasn't set one up yet
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

    # Allowed values
    if asset not in ("USDT", "USDC"):
        return jsonify({"message": "asset must be USDT or USDC"}), 400
    if network not in ("TRC20", "ERC20", "BASE", "BEP20"):
        return jsonify({"message": "network must be TRC20, ERC20, BASE, or BEP20"}), 400

    # Validate address when crypto acceptance is being turned ON
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
    """
    Create a Daya funding account for a crypto payment.

    Request body:
      orderId    — comma-separated order ID(s) from checkout
      amountNgn  — total NGN amount the buyer owes
      buyerEmail — buyer's email
      buyerName  — buyer's full name
      type       — "ngn_onramp" | "crypto_direct"
      asset      — "USDT" | "USDC"  (required for crypto_direct)
      network    — "TRC20" | "ERC20" | "BASE" | "BEP20" (required for crypto_direct)

    Returns payment instructions the frontend CryptoPaymentModal displays.
    """
    buyer_user_id = int(get_jwt_identity())
    data = request.get_json() or {}

    order_id_param = data.get("orderId") or data.get("order_id", "")
    # Take first order id for tracking (multi-vendor → comma-separated)
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

    buyer_email = (data.get("buyerEmail") or data.get("buyer_email") or "").strip()
    buyer_name  = (data.get("buyerName")  or data.get("buyer_name")  or "").strip()
    payment_type = data.get("type", "ngn_onramp")
    asset        = data.get("asset", "USDT")
    network      = data.get("network", "TRC20")

    if payment_type not in ("ngn_onramp", "crypto_direct"):
        return jsonify({"message": "type must be ngn_onramp or crypto_direct"}), 400
    if asset not in ("USDT", "USDC"):
        return jsonify({"message": "asset must be USDT or USDC"}), 400
    if not _key_configured():
        return jsonify({"message": "Crypto payments are not configured yet"}), 503

    # Verify the order belongs to this buyer
    order = db.session.get(Order, primary_order_id)
    if not order:
        return jsonify({"message": "Order not found"}), 404
    if order.buyer_id != buyer_user_id:
        return jsonify({"message": "Unauthorized"}), 403

    # Reuse existing DayaPayment if the order already has one (idempotent)
    existing = DayaPayment.query.filter_by(
        order_id=primary_order_id,
        payment_type=payment_type,
    ).filter(DayaPayment.status.in_(["PENDING", "RECEIVED", "REQUIRES_REVIEW"])).first()

    if existing and existing.daya_funding_account_id:
        # Rate may have expired — check and allow re-initiation if so
        if existing.rate_expires_at and existing.rate_expires_at > _utcnow():
            return _build_initiate_response(existing), 200
        # Rate expired — fall through to create a new one

    # ── Step 1: Get or create Daya customer for the buyer ────────────────────
    try:
        name_parts = buyer_name.split(" ", 1)
        first = name_parts[0] if name_parts else buyer_email.split("@")[0]
        last  = name_parts[1] if len(name_parts) > 1 else ""
        daya_customer_id = daya_service.get_or_create_customer(buyer_email, first, last)
    except RuntimeError as exc:
        logger.error("[DAYA INITIATE] Customer creation failed: %s", exc)
        return jsonify({"message": f"Could not create payment session: {exc}"}), 502

    # ── Step 2: Fetch a firm rate ─────────────────────────────────────────────
    try:
        side = "BUY" if payment_type == "ngn_onramp" else "SELL"
        rate_data = daya_service.get_rate(asset=asset, side=side)
    except RuntimeError as exc:
        logger.error("[DAYA INITIATE] Rate fetch failed: %s", exc)
        return jsonify({"message": f"Could not fetch exchange rate: {exc}"}), 502

    rate_id    = rate_data["rate_id"]
    rate       = float(rate_data["rate"])          # NGN per 1 stablecoin
    expires_at = rate_data["expires_at"]           # ISO 8601 string

    # Unique idempotency key per order + type
    idem_key = f"siiqo-{primary_order_id}-{payment_type}-{rate_id}"

    # ── Step 3: Create Daya funding account ──────────────────────────────────
    try:
        if payment_type == "ngn_onramp":
            fa = daya_service.create_ngn_funding_account(
                customer_id     = daya_customer_id,
                amount_ngn      = int(round(amount_ngn)),
                rate_id         = rate_id,
                idempotency_key = idem_key,
                developer_fee_pct = "0",  # Siiqo's 6% is already in the NGN price
            )
            instructions = fa.get("instructions", [{}])[0]
            bank_name      = instructions.get("bank_name", "")
            account_number = instructions.get("account_number", "")
            account_name   = instructions.get("account_name", "")
            wallet_addr    = None
            amount_crypto  = None
        else:
            # crypto_direct: calculate equivalent crypto amount from rate
            crypto_amount  = round(amount_ngn / rate, 6)
            amount_crypto  = f"{crypto_amount:.6f}".rstrip("0").rstrip(".")
            fa = daya_service.create_crypto_funding_account(
                customer_id     = daya_customer_id,
                asset           = asset,
                network         = network,
                rate_id         = rate_id,
                idempotency_key = idem_key,
                developer_fee_pct = "0",
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

    # ── Step 4: Persist DayaPayment record ───────────────────────────────────
    # Upsert: update existing PENDING row or create new
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
    else:
        dp = DayaPayment(
            order_id                = primary_order_id,
            buyer_id                = buyer_user_id,
            payment_type            = payment_type,
            daya_funding_account_id = fa_id,
            daya_rate_id            = rate_id,
            rate_expires_at         = expires_dt,
            amount_ngn              = amount_ngn,
            amount_crypto           = amount_crypto,
            asset                   = asset,
            network                 = network,
            bank_name               = bank_name,
            account_number          = account_number,
            account_name            = account_name,
            wallet_address          = wallet_addr,
            status                  = "PENDING",
            rate                    = rate,
        )
        db.session.add(dp)

    db.session.commit()
    return _build_initiate_response(dp), 200


def _key_configured() -> bool:
    return bool(os.environ.get("DAYA_API_KEY", ""))


def _build_initiate_response(dp: DayaPayment) -> tuple:
    """Serialise a DayaPayment into the JSON shape the frontend expects."""
    payload = {
        "type":               dp.payment_type,
        "fundingAccountId":   dp.daya_funding_account_id,
        "rateId":             dp.daya_rate_id,
        "expiresAt":          dp.rate_expires_at.isoformat() if dp.rate_expires_at else None,
        "amountNgn":          float(dp.amount_ngn),
        "orderId":            str(dp.order_id),
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
@jwt_required()
def daya_status():
    """
    Poll the Daya deposit status for an order.
    Called by the frontend every 5 seconds while the modal is open.

    Returns:
      { orderId, status, paidAt, amountPaid, currency }
    Status values: PENDING | RECEIVED | PROCESSING | COMPLETED | FAILED | EXPIRED
    """
    buyer_user_id = int(get_jwt_identity())
    order_id_str  = request.args.get("order_id", "")

    try:
        order_id = int(order_id_str.split(",")[0])
    except (ValueError, IndexError):
        return jsonify({"message": "order_id is required"}), 400

    dp = DayaPayment.query.filter_by(order_id=order_id).first()
    if not dp:
        return jsonify({"message": "No crypto payment found for this order"}), 404

    # Security: only the buyer can poll
    if dp.buyer_id != buyer_user_id:
        return jsonify({"message": "Unauthorized"}), 403

    # If already terminal, return cached DB status immediately
    if dp.status in ("COMPLETED", "FAILED"):
        return jsonify({
            "orderId": str(order_id),
            "status":  dp.status,
            "paidAt":  dp.updated_at.isoformat() if dp.status == "COMPLETED" else None,
        }), 200

    # Check rate expiry — if expired and still PENDING, return EXPIRED
    if dp.rate_expires_at and dp.rate_expires_at < _utcnow() and dp.status == "PENDING":
        dp.status     = "EXPIRED"
        dp.updated_at = _utcnow()
        db.session.commit()
        return jsonify({"orderId": str(order_id), "status": "EXPIRED"}), 200

    # Live poll from Daya
    if dp.daya_funding_account_id and _key_configured():
        try:
            deposit = daya_service.get_deposit_by_funding_account(dp.daya_funding_account_id)
            if deposit:
                new_status = daya_service._map_daya_status(deposit.get("status", ""))
                dp.daya_deposit_id = deposit.get("id")
                if new_status != dp.status:
                    dp.status     = new_status
                    dp.updated_at = _utcnow()
                    db.session.commit()

                    # If COMPLETED, trigger order confirmation
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
    """
    Refresh an expired Daya rate for an existing pending payment.
    Called when the 30-minute rate lock expires before the buyer pays.

    Request: { orderId, fundingAccountId }
    Response: { rate, rateId, expiresAt, amountNgn?, amountCrypto? }
    """
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

    # Recalculate crypto amount if applicable
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

    response = {
        "rate":       rate,
        "rateId":     rate_id,
        "expiresAt":  expires_at,
    }
    if new_amount_crypto:
        response["amountCrypto"] = new_amount_crypto
    else:
        response["amountNgn"] = float(dp.amount_ngn)

    return jsonify(response), 200


# ===========================================================================
# POST /payments/daya/webhook  — no JWT, HMAC verified
# ===========================================================================

@payments_bp.route("/daya/webhook", methods=["POST"])
def daya_webhook():
    """
    Receive Daya deposit lifecycle events.

    Events we handle:
      deposit.completed  → mark DayaPayment COMPLETED, confirm order

    All others are acknowledged (200) and ignored.
    Signature is verified via X-Daya-Signature (HMAC-SHA256).
    """
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

        if funding_account_id:
            dp = DayaPayment.query.filter_by(
                daya_funding_account_id=funding_account_id
            ).first()
            if dp and dp.status not in ("COMPLETED", "FAILED"):
                dp.status          = "COMPLETED"
                dp.daya_deposit_id = deposit_id
                dp.updated_at      = _utcnow()
                db.session.commit()
                _handle_crypto_payment_confirmed(dp.order_id, dp)

    return jsonify({"received": True}), 200


# ===========================================================================
# Internal helper — trigger order confirmation for a completed crypto payment
# ===========================================================================

def _handle_crypto_payment_confirmed(order_id: int, dp: DayaPayment):
    """
    Called when Daya confirms a crypto deposit is COMPLETED.

    Steps:
    1. Mark order as PAID
    2. Create / update EscrowTransaction → IN_ESCROW then immediately
       call digital/service delivery helpers (auto-release for digital products)
    3. Send buyer confirmation notification
    """
    from app.models.escrow import EscrowTransaction, EscrowStatus
    from app.models.communication import Notification

    try:
        order = db.session.get(Order, order_id)
        if not order:
            logger.error("[DAYA CONFIRM] Order %s not found", order_id)
            return
        if order.status in ("PAID", "COMPLETED", "RELEASED"):
            logger.info("[DAYA CONFIRM] Order %s already confirmed — skipping", order_id)
            return

        order.status         = "PAID"
        order.payment_method = "CRYPTO"

        # Upsert EscrowTransaction so the existing escrow status machinery works
        escrow = EscrowTransaction.query.filter_by(order_id=order_id).first()
        if not escrow:
            fee_amount = round(float(order.total_amount) * 0.06, 2)
            txn_number = f"DYA-{uuid.uuid4().hex[:12].upper()}"
            escrow = EscrowTransaction(
                order_id           = order_id,
                transaction_number = txn_number,
                status             = EscrowStatus.IN_ESCROW,
                amount             = float(order.total_amount),
                fee_percent        = 6.00,
                fee_amount         = fee_amount,
                payment_link       = None,
                payscrow_transaction_id = f"DAYA-{dp.daya_deposit_id or dp.daya_funding_account_id}",
            )
            escrow.paid_at = _utcnow()
            db.session.add(escrow)
        else:
            escrow.status  = EscrowStatus.IN_ESCROW
            escrow.paid_at = escrow.paid_at or _utcnow()
            if dp.daya_deposit_id:
                escrow.payscrow_transaction_id = f"DAYA-{dp.daya_deposit_id}"

        db.session.flush()

        # For digital/service products auto-release immediately (same as Paystack flow)
        from app.routes.escrow import _deliver_digital_products, _deliver_service_products
        is_digital  = _deliver_digital_products(order, escrow)
        is_service  = False
        if not is_digital:
            is_service = _deliver_service_products(order, escrow)

        if not is_digital and not is_service:
            # Physical order — notify vendor to ship
            db.session.add(Notification(
                user_id  = order.vendor_id,
                title    = "Crypto Payment Received",
                message  = f"Crypto payment confirmed for Order #{order_id}. Please ship the order.",
                type     = "ESCROW",
                order_id = order_id,
            ))
            db.session.add(Notification(
                user_id  = order.buyer_id,
                title    = "Payment Confirmed",
                message  = f"Your crypto payment for Order #{order_id} has been confirmed.",
                type     = "ORDER",
                order_id = order_id,
            ))

        db.session.commit()
        logger.info("[DAYA CONFIRM] Order %s confirmed (digital=%s service=%s)",
                    order_id, is_digital, is_service)

    except Exception as exc:
        db.session.rollback()
        logger.error("[DAYA CONFIRM] Error confirming Order %s: %s", order_id, exc)


# ===========================================================================
# Pro subscription  — POST /payments/initiate-pro-subscription
# Already handled in bridge.py; keeping an alias here so either URL works
# ===========================================================================

@payments_bp.route("/initiate-pro-subscription", methods=["POST"])
@jwt_required()
def initiate_pro_subscription():
    """Delegates to the existing bridge.py handler."""
    from app.routes.bridge import initiate_pro_subscription as _bridge_sub
    return _bridge_sub()
