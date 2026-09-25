# payments.py - Daya crypto payment routes + vendor crypto wallet management + Flutterwave
#
# Routes registered at /api/payments/* via payments_bp:
#   GET  /payments/vendor/crypto-wallet        -> get current wallet settings
#   POST /payments/vendor/crypto-wallet        -> save / update wallet settings
#   POST /payments/daya/initiate               -> create Daya funding account
#   GET  /payments/daya/status?order_id=X      -> poll deposit status
#   POST /payments/daya/refresh-rate           -> refresh expired rate
#   POST /payments/daya/webhook                -> deposit lifecycle events (no JWT)
#   POST /payments/initiate-pro-subscription   -> delegates to bridge.py
#   POST /payments/flutterwave/initiate        -> Flutterwave hosted checkout (digital, service, events)
#   POST /payments/flutterwave/webhook         -> Flutterwave webhook (no JWT)
#   GET  /payments/flutterwave/status          -> check/proactively verify Flutterwave transaction

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
@jwt_required(optional=True)
def daya_initiate():
    """Create a Daya funding account for a crypto payment (supports logged-in users and guests)."""
    user_id = get_jwt_identity()
    buyer_user_id = int(user_id) if user_id else None
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

    buyer_email  = (data.get("buyerEmail") or data.get("buyer_email") or "").strip().lower()
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
    
    # Check authorization if order is tied to a specific registered user
    if order.buyer_id is not None and buyer_user_id is not None and order.buyer_id != buyer_user_id:
        return jsonify({"message": "Unauthorized"}), 403

    if not buyer_email:
        if order.buyer_email:
            buyer_email = order.buyer_email.strip().lower()
        elif order.buyer and order.buyer.email:
            buyer_email = order.buyer.email.strip().lower()

    if not buyer_email or "@" not in buyer_email:
        return jsonify({"message": "A valid email address is required to initiate payment."}), 400

    if not buyer_name:
        if order.buyer_name:
            buyer_name = order.buyer_name.strip()
        elif order.buyer:
            buyer_name = f"{order.buyer.first_name or ''} {order.buyer.last_name or ''}".strip()
        if not buyer_name:
            buyer_name = buyer_email.split("@")[0]

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

    if dp.status == "FAILED":
        return jsonify({
            "orderId": str(order_id),
            "status":  dp.status,
            "paidAt":  None,
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
                # ── FUNDING ACCOUNT OWNERSHIP GUARD ───────────────────────────
                # A deposit is only valid for THIS order if the deposit's
                # funding_account_id exactly matches the DayaPayment row's
                # daya_funding_account_id. This is a hard, exact match — it
                # prevents a deposit created for order A from confirming order B
                # even when both orders were created within the same rate window.
                #
                # Additionally we require the deposit was created AFTER this
                # payment session started (with a 60-second grace for clock skew)
                # to protect against stale deposits on reused PERMANENT crypto
                # funding accounts.
                deposit_funding_account_id = (
                    deposit.get("funding_account_id")
                    or deposit.get("fundingAccountId")
                    or ""
                )
                deposit_belongs_to_this_order = (
                    deposit_funding_account_id == dp.daya_funding_account_id
                )

                if not deposit_belongs_to_this_order:
                    logger.info(
                        "[DAYA STATUS] Order %s — deposit %s belongs to funding account %s, "
                        "not this order's account %s. Ignoring.",
                        order_id, deposit.get("id"),
                        deposit_funding_account_id, dp.daya_funding_account_id,
                    )
                else:
                    # Funding account matches — also verify the deposit is fresh
                    # (guards against old COMPLETED deposits on PERMANENT crypto accounts)
                    deposit_created_raw = deposit.get("created_at") or deposit.get("createdAt", "")
                    deposit_is_fresh = True  # default trust when no timestamp available
                    if deposit_created_raw and dp.created_at:
                        try:
                            from datetime import datetime as _dt2, timedelta
                            dep_ts = _dt2.fromisoformat(
                                deposit_created_raw.replace("Z", "+00:00")
                            )
                            # 60-second grace period for clock skew
                            cutoff = dp.created_at.replace(tzinfo=timezone.utc) - timedelta(seconds=60)
                            deposit_is_fresh = dep_ts >= cutoff
                        except Exception:
                            deposit_is_fresh = True  # can't parse — trust the account match

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

    if dp.status == "COMPLETED":
        # Self-healing: If payment already completed (e.g. from webhook) but order
        # failed to confirm due to transient error/crash, re-trigger confirmation now
        try:
            from app.models.order import Order
            _ord = db.session.get(Order, order_id)
            if _ord and _ord.status not in ("PAID", "COMPLETED", "RELEASED"):
                logger.info("[DAYA STATUS] Order %s is %s but DayaPayment is COMPLETED — retrying confirmation",
                            order_id, _ord.status)
                _handle_crypto_payment_confirmed(order_id, dp)
        except Exception as exc:
            logger.warning("[DAYA STATUS] Retry confirmation failed for order %s: %s", order_id, exc)

    from app.models.order import Order
    from app.routes.escrow import generate_order_token
    _o = db.session.get(Order, order_id)
    b_phone = (_o.buyer_phone or getattr(_o, 'delivery_phone', None)) if _o else None
    b_email = _o.buyer_email if _o else None
    is_existing = bool(_o and not _o.is_guest) if _o else False
    conf_url = f"https://siiqo.com/order-confirm/{order_id}?token={generate_order_token(order_id)}"

    pl_file_url = None
    pl_product_type = None
    if _o and _o.payment_link_id:
        from app.models.payment_link import PaymentLink
        pl = db.session.get(PaymentLink, _o.payment_link_id)
        if pl:
            pl_file_url = pl.file_url
            pl_product_type = pl.product_type
    elif _o and _o.items:
        has_phys = any((getattr(it.product, 'product_type', '') or 'physical').lower() == 'physical' for it in _o.items if it.product)
        has_serv = any((getattr(it.product, 'product_type', '') or '').lower() == 'service' for it in _o.items if it.product)
        if has_phys:
            pl_product_type = 'physical'
        elif has_serv:
            pl_product_type = 'service'
        else:
            pl_product_type = 'digital'

    return jsonify({
        "orderId": str(order_id),
        "status":  dp.status,
        "paidAt":  dp.updated_at.isoformat() if dp.status == "COMPLETED" else None,
        "confirmation_url": conf_url,
        "buyer_phone": b_phone,
        "buyer_email": b_email,
        "is_existing_account": is_existing,
        "file_url": pl_file_url,
        "product_type": pl_product_type,
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
                # ── FUNDING ACCOUNT OWNERSHIP GUARD (webhook) ─────────────────
                # The deposit's funding_account_id already matched dp above via
                # the query filter. Verify the deposit is fresh relative to this
                # payment session — protects against stale deposits on PERMANENT
                # crypto accounts being redelivered by Daya.
                deposit_is_fresh = True  # default: webhooks come directly from Daya
                if deposit_created_raw and dp.created_at:
                    try:
                        from datetime import datetime as _dt2, timedelta
                        dep_ts = _dt2.fromisoformat(
                            deposit_created_raw.replace("Z", "+00:00")
                        )
                        # 60-second grace period for clock skew
                        cutoff = dp.created_at.replace(tzinfo=timezone.utc) - timedelta(seconds=60)
                        deposit_is_fresh = dep_ts >= cutoff
                    except Exception:
                        # Can't parse timestamp — trust the webhook since it came
                        # directly from Daya and the funding_account_id matched exactly
                        logger.warning(
                            "[DAYA WEBHOOK] Could not parse deposit created_at=%s for order %s. "
                            "Proceeding cautiously.",
                            deposit_created_raw, dp.order_id
                        )
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
            else:
                # Check if this funding account belongs to a storefront verification fee
                try:
                    from app.models.user import Storefront
                    from app.models.communication import Notification
                    all_sfs = Storefront.query.filter(Storefront.template_options.isnot(None)).all()
                    for sf_item in all_sfs:
                        opts = sf_item.template_options or {}
                        if opts.get("daya_verification", {}).get("funding_account_id") == funding_account_id:
                            sf_item.verification_status = 'PENDING_ADMIN_REVIEW'
                            db.session.add(Notification(
                                user_id=sf_item.vendor_id,
                                title="Verification Application Received 🛡️",
                                message=(
                                    "Your verification payment of ₦2,500 via Daya transfer has been received. "
                                    "Our compliance team will review your identity documents within 48 hours."
                                ),
                                type="ACCOUNT",
                            ))
                            db.session.commit()
                            logger.info("[DAYA WEBHOOK] Verification deposit %s applied to storefront %s (vendor %s)",
                                        deposit_id, sf_item.id, sf_item.vendor_id)
                            break
                except Exception as exc:
                    logger.warning("[DAYA WEBHOOK] Error matching verification storefront: %s", exc)

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
            fee_rate = 0.03  # Flat 3% Safe Pay fee for all vendors
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
                # Digital Pay Link — release immediately, credit vendor, single payout
                net_amount = float(escrow.amount) - float(escrow.fee_amount or 0)
                escrow.status = EscrowStatus.RELEASED
                escrow.released_at = _utcnow()
                order.status = 'COMPLETED'
                if _link: _link.status = 'PAID'
                # Write ledger credit BEFORE attempting payout so the balance
                # is correct even if the payout transfer call fails or is retried
                from app.routes.escrow import _credit_vendor_ledger
                _credit_vendor_ledger(
                    vendor_id=order.vendor_id,
                    amount=net_amount,
                    reference_id=escrow.transaction_number,
                    description=f"Payout for Order #{order_id} (guest_buyer_token)",
                )
                if order.buyer_id:
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
                # Email buyer with file URL from the Pay Link record
                try:
                    from app.utils.email import send_siiqo_email
                    from app.models.user import User as _U
                    _buyer = db.session.get(_U, order.buyer_id) if order.buyer_id else None
                    _buyer_email = (_buyer.email if _buyer else None) or getattr(order, 'buyer_email', None)
                    if _buyer_email:
                        _first = (_buyer.first_name if _buyer else None) or getattr(order, 'buyer_name', None) or "there"
                        _file_url = getattr(_link, 'file_url', None)
                        _link_html = (
                            f'<p style="margin:8px 0;"><a href="{_file_url}" '
                            f'style="color:#E0921C;word-break:break-all;">{_file_url}</a></p>'
                            if _file_url
                            else "<p>The vendor will send your download link shortly.</p>"
                        )
                        send_siiqo_email(
                            to_email=_buyer_email,
                            subject=f"Your Digital Download – Order #{order_id} | Siiqo",
                            template_name="system_notice",
                            first_name=_first,
                            notice_text=(
                                f"Your payment for Order #{order_id} is confirmed.<br><br>"
                                f"Here is your download link:<br><br>{_link_html}<br>"
                                "If you have any issues, please contact the seller via Siiqo chat."
                            ),
                        )
                except Exception as _e:
                    logger.warning("[DAYA CONFIRM] Pay Link digital buyer email failed Order #%s: %s", order_id, _e)
                is_digital = True
            elif _ltype == 'service':
                # Service Pay Link — release immediately, credit vendor, single payout
                net_amount = float(escrow.amount) - float(escrow.fee_amount or 0)
                escrow.status = EscrowStatus.RELEASED
                escrow.released_at = _utcnow()
                order.status = 'COMPLETED'
                if _link: _link.status = 'PAID'
                # Write ledger credit BEFORE attempting payout
                from app.routes.escrow import _credit_vendor_ledger
                _credit_vendor_ledger(
                    vendor_id=order.vendor_id,
                    amount=net_amount,
                    reference_id=escrow.transaction_number,
                    description=f"Payout for Order #{order_id} (guest_buyer_token)",
                )
                if order.buyer_id:
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
                # Email buyer with booking/session link from the Pay Link record
                try:
                    from app.utils.email import send_siiqo_email
                    from app.models.user import User as _U
                    _buyer = db.session.get(_U, order.buyer_id) if order.buyer_id else None
                    _buyer_email = (_buyer.email if _buyer else None) or getattr(order, 'buyer_email', None)
                    if _buyer_email:
                        _first = (_buyer.first_name if _buyer else None) or getattr(order, 'buyer_name', None) or "there"
                        _booking_url = getattr(_link, 'file_url', None)
                        _link_html = (
                            f'<p style="margin:8px 0;"><a href="{_booking_url}" '
                            f'style="color:#E0921C;word-break:break-all;">{_booking_url}</a></p>'
                            if _booking_url
                            else "<p>The vendor will reach out to schedule your service.</p>"
                        )
                        send_siiqo_email(
                            to_email=_buyer_email,
                            subject=f"Service Booking Confirmed – Order #{order_id} | Siiqo",
                            template_name="system_notice",
                            first_name=_first,
                            notice_text=(
                                f"Your payment for Order #{order_id} is confirmed.<br><br>"
                                f"Use the link below to access your service:<br><br>{_link_html}<br>"
                                "If you have any issues, please contact the seller via Siiqo chat."
                            ),
                        )
                except Exception as _e:
                    logger.warning("[DAYA CONFIRM] Pay Link service buyer email failed Order #%s: %s", order_id, _e)
                is_service = True
            # else: physical Pay Link — falls through to hold-in-escrow logic below
            # Mark INVOICE as PAID regardless of product type once payment confirmed
            if _link and _link.link_type == 'INVOICE' and not is_digital and not is_service:
                _link.status = 'PAID'

        # ── Vendor payout via Daya for digital/service orders ─────────────
        # Called exactly once here for ALL digital/service paths (both product-
        # based orders handled by _deliver_* and Pay Link orders handled above).
        # Physical orders are paid out when the buyer confirms delivery.
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
            if order.buyer_id:
                db.session.add(Notification(
                    user_id=order.buyer_id,
                    title="Payment Confirmed",
                    message=buyer_notif_msg,
                    type="ORDER",
                    order_id=order_id,
                ))
            
            # ── Send emails for physical Daya orders ─────────────────────────────
            from app.utils.email import send_siiqo_email
            from app.models.user import User
            
            vendor = db.session.get(User, order.vendor_id)
            if vendor and vendor.email:
                try:
                    send_siiqo_email(
                        to_email=vendor.email,
                        subject=f"Payment Received - Order #{order_id}",
                        template_name="system_notice",
                        first_name=vendor.first_name or "Vendor",
                        notice_text=(
                            f"{'Bank transfer' if is_naira else 'Crypto'} payment confirmed "
                            f"for Order #{order_id}.<br><br>"
                            f"<strong>₦{float(order.total_amount):,.2f}</strong> is now held in escrow.<br><br>"
                            f"Please ship the order. Once the buyer confirms delivery, funds will be released to you.<br><br>"
                            f"<a href='https://siiqo.com/vendor/orders'>View & ship order →</a>"
                        ),
                    )
                except Exception as e:
                    logger.warning(f"[EMAIL WARN] Daya physical vendor email failed Order #{order_id}: {e}")
            
            buyer = db.session.get(User, order.buyer_id) if order.buyer_id else None
            buyer_email = (buyer.email if buyer else None) or getattr(order, 'buyer_email', None)
            if buyer_email:
                buyer_name = (buyer.first_name if buyer else None) or getattr(order, 'buyer_name', None) or "Customer"
                try:
                    send_siiqo_email(
                        to_email=buyer_email,
                        subject=f"Payment Confirmed - Order #{order_id}",
                        template_name="system_notice",
                        first_name=buyer_name,
                        notice_text=(
                            f"Your {'bank transfer' if is_naira else 'crypto'} payment for Order #{order_id} "
                            f"has been confirmed.<br><br>"
                            f"Your order is now being prepared for delivery. You'll be notified when it ships.<br><br>"
                            f"<a href='https://siiqo.com/orders/{order_id}'>Track your order →</a>"
                        ),
                    )
                except Exception as e:
                    logger.warning(f"[EMAIL WARN] Daya physical buyer email failed Order #{order_id}: {e}")

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
    rate_asset = (dp.asset if (dp and dp.asset) else "USDT")
    try:
        rate_data = daya_service.get_rate(asset=rate_asset, side="SELL")
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
        notif_msg = (
            f"Your order #{order.id} is complete and payment is confirmed. "
            "To receive your payout, please add your bank account in "
            "Escrow & Payouts (/vendor/escrow). Contact support@siiqo.com if you need help."
        )
        db.session.add(Notification(
            user_id=order.vendor_id,
            title="Payout Pending — Add Bank Account",
            message=notif_msg,
            type="ESCROW",
            order_id=order.id,
        ))
        db.session.commit()

        # Send email alert to vendor if email exists
        from app.models.user import User
        vendor_user = order.vendor if hasattr(order, 'vendor') and order.vendor else db.session.get(User, order.vendor_id)
        if vendor_user and vendor_user.email:
            try:
                from app.utils.email import send_siiqo_email
                send_siiqo_email(
                    to_email=vendor_user.email,
                    subject=f"Action Required: Add bank account to receive payout for Order #{order.id}",
                    template_name="base_notification",
                    first_name=vendor_user.first_name or "Vendor",
                    title="Payout Pending — Add Bank Account",
                    message=notif_msg,
                    cta_text="Add Bank Details",
                    cta_url="https://siiqo.com/vendor/escrow",
                )
            except Exception as _em_err:
                logger.info("[EMAIL] Could not send payout bank alert email: %s", _em_err)
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


# ===========================================================================
# FLUTTERWAVE PAYMENT ROUTES (Digital, Services & Events)
# ===========================================================================

@payments_bp.route("/flutterwave/initiate", methods=["POST"])
@jwt_required(optional=True)
def flutterwave_initiate():
    """
    Initiate a Flutterwave checkout for digital products, services, and event tickets.
    Supports single and multi-order carts (unified payment).
    """
    from app.services import flutterwave_service as _flw
    from app.models.escrow import EscrowTransaction, EscrowStatus
    from app.models.withdrawal import VendorBankAccount
    from app.models.product import Product as _Prod

    user_id = get_jwt_identity()
    data = request.get_json() or {}

    order_id_param = data.get("orderId") or data.get("order_id", "")
    order_ids = [int(oid) for oid in str(order_id_param).split(",") if oid.strip().isdigit()]
    if not order_ids:
        return jsonify({"message": "orderId is required"}), 400

    orders = Order.query.filter(Order.id.in_(order_ids)).all()
    if not orders:
        return jsonify({"message": "Orders not found"}), 404

    # Verify authorization if user is authenticated
    for order in orders:
        if order.buyer_id and user_id and str(order.buyer_id) != str(user_id):
            return jsonify({"message": "Unauthorized"}), 403

    # Check product types: Flutterwave here is strictly for digital, services, and events
    # Physical products remain with Daya / POD
    from app.models.event import TicketPurchase
    for order in orders:
        # 1. Event orders have associated TicketPurchase records and are strictly non-physical
        has_tickets = TicketPurchase.query.filter_by(order_id=order.id).first() is not None
        if has_tickets:
            continue

        # 2. Check regular catalog items
        for item in (order.items or []):
            p = db.session.get(_Prod, item.product_id) if item.product_id else item.product
            p_type = (getattr(p, "product_type", None) or "physical").lower() if p else None
            if p_type == "physical":
                return jsonify({
                    "message": (
                        "Flutterwave card checkout is currently enabled for digital products, "
                        "service bookings, and event tickets. For physical products, please use "
                        "Bank Transfer & Crypto (Daya) or Pay on Delivery."
                    ),
                    "code": "FLUTTERWAVE_PHYSICAL_BLOCKED",
                }), 400

    # Calculate total listed price & Siiqo 3% fee
    total_listed_ngn = sum(float(o.total_amount) for o in orders)
    if total_listed_ngn <= 0:
        return jsonify({"message": "Total amount must be greater than zero"}), 400

    siiqo_fee_total = round(total_listed_ngn * 0.03, 2)  # Flat 3% Safe Pay fee

    # Buyer contact info
    primary_order = orders[0]
    buyer_email = (
        (data.get("buyerEmail") or data.get("buyer_email") or "").strip().lower()
        or (primary_order.buyer_email or "").strip().lower()
        or (primary_order.buyer.email if primary_order.buyer else "").strip().lower()
    )
    buyer_name = (
        (data.get("buyerName") or data.get("buyer_name") or "").strip()
        or (primary_order.buyer_name or "").strip()
        or (f"{primary_order.buyer.first_name or ''} {primary_order.buyer.last_name or ''}".strip() if primary_order.buyer else "")
        or "Siiqo Buyer"
    )
    buyer_phone = (
        (data.get("buyerPhone") or data.get("buyer_phone") or "").strip()
        or (primary_order.buyer_phone or getattr(primary_order, "delivery_phone", "") or "").strip()
        or "08012345678"
    )

    if not buyer_email or "@" not in buyer_email:
        return jsonify({"message": "A valid email address is required to initiate payment."}), 400

    # Look up vendor subaccount for single-vendor carts
    vendor_ids = list({o.vendor_id for o in orders})
    flw_subaccount_id = None
    if len(vendor_ids) == 1:
        v_id = vendor_ids[0]
        bank_acc = VendorBankAccount.query.filter_by(vendor_id=v_id, is_default=True).first() or \
                   VendorBankAccount.query.filter_by(vendor_id=v_id).first()
        if bank_acc and bank_acc.flw_subaccount_id:
            flw_subaccount_id = bank_acc.flw_subaccount_id
        elif bank_acc and bank_acc.account_number and bank_acc.bank_code and _flw.is_configured():
            # Auto-provision Flutterwave subaccount if vendor already has bank details
            try:
                from app.models.user import Storefront, User as _U
                sf = Storefront.query.filter_by(vendor_id=v_id).first()
                vendor_u = db.session.get(_U, v_id)
                v_phone = (vendor_u.phone if vendor_u else "") or "08012345678"
                b_name = sf.store_name if sf else (vendor_u.full_name if vendor_u else f"Vendor {v_id}")
                created_sub = _flw.create_subaccount(
                    business_name=b_name,
                    bank_code=bank_acc.bank_code,
                    account_number=bank_acc.account_number,
                    business_mobile=v_phone,
                    business_email=sf.contact_email if (sf and sf.contact_email) else "",
                )
                if created_sub.get("success"):
                    flw_subaccount_id = created_sub["subaccount_id"]
                    bank_acc.flw_subaccount_id = flw_subaccount_id
                    db.session.commit()
            except Exception as _sub_err:
                logger.warning("[FLW INITIATE] On-the-fly subaccount creation skipped: %s", _sub_err)

    site_url = os.environ.get("SITE_URL", "https://siiqo.com").rstrip("/")
    primary_id = primary_order.id
    order_ids_str = ",".join(str(oid) for oid in order_ids)
    tx_ref = f"FLW-ORD-{primary_id}-{uuid.uuid4().hex[:8].upper()}"

    custom_return_url = data.get("returnUrl") or data.get("return_url")
    redirect_url = custom_return_url or f"{site_url}/payment/success?reference={tx_ref}&order_id={primary_id}"

    # Narration for hosted receipt
    narration = f"Siiqo Order #{primary_id}" if len(order_ids) == 1 else f"Siiqo Orders {', '.join(f'#{i}' for i in order_ids)}"

    flw_result = _flw.initiate_payment(
        order_id=order_ids_str,
        listed_price_ngn=total_listed_ngn,
        siiqo_fee_ngn=siiqo_fee_total,
        buyer_email=buyer_email,
        buyer_name=buyer_name,
        buyer_phone=buyer_phone,
        flw_subaccount_id=flw_subaccount_id,
        tx_ref=tx_ref,
        redirect_url=redirect_url,
        is_international=bool(data.get("isInternational", False)),
        narration=narration,
    )

    if not flw_result.get("success"):
        return jsonify({"message": flw_result.get("error_message") or "Could not initialize Flutterwave payment."}), 400

    # Create or update EscrowTransaction for each order
    for o in orders:
        o.payment_method = "FLUTTERWAVE"
        indiv_fee = round(float(o.total_amount) * 0.03, 2)
        escrow = EscrowTransaction.query.filter_by(order_id=o.id).first()
        if not escrow:
            escrow = EscrowTransaction(
                order_id=o.id,
                transaction_number=tx_ref,
                status=EscrowStatus.PENDING_PAYMENT,
                amount=float(o.total_amount),
                fee_percent=3.0,
                fee_amount=indiv_fee,
                payment_link=flw_result["payment_link"],
                payscrow_transaction_id=tx_ref,
                payscrow_ref=tx_ref,
            )
            db.session.add(escrow)
        else:
            escrow.transaction_number = tx_ref
            escrow.status = EscrowStatus.PENDING_PAYMENT
            escrow.payment_link = flw_result["payment_link"]
            escrow.payscrow_transaction_id = tx_ref
            escrow.payscrow_ref = tx_ref

    db.session.commit()

    return jsonify({
        "success": True,
        "paymentLink": flw_result["payment_link"],
        "transactionNumber": tx_ref,
        "amount": flw_result["buyer_total"],
        "listedAmount": total_listed_ngn,
        "flwFee": flw_result["flw_fee"],
        "orderId": order_ids_str,
    }), 200


@payments_bp.route("/flutterwave/webhook", methods=["POST"])
def flutterwave_webhook():
    """
    Receive and handle Flutterwave webhook events.
    Verifies the verif-hash header, validates the transaction with Flutterwave API,
    and completes order fulfillment.
    """
    from app.services import flutterwave_service as _flw

    raw_body = request.get_data()
    sig_header = request.headers.get("verif-hash", "")

    if not _flw.verify_webhook_signature(raw_body, sig_header):
        logger.warning("[FLW WEBHOOK] Invalid signature hash")
        return jsonify({"message": "Invalid signature"}), 401

    try:
        event_data = request.get_json(force=True) or {}
    except Exception:
        return jsonify({"message": "Invalid JSON"}), 400

    data = event_data.get("data", {})
    tx_id = data.get("id")
    tx_ref = data.get("tx_ref") or ""
    status = data.get("status")

    logger.info("[FLW WEBHOOK] event=%s tx_id=%s status=%s ref=%s",
                event_data.get("event"), tx_id, status, tx_ref)

    if not tx_id:
        return jsonify({"status": "ignored", "message": "No transaction ID"}), 200

    # Query Flutterwave API directly to guarantee authenticity
    verification = _flw.verify_transaction(tx_id)
    if not verification.get("success") or verification.get("status") != "successful":
        logger.warning("[FLW WEBHOOK] Verification check failed for tx_id %s: %s",
                       tx_id, verification.get("error_message"))
        return jsonify({"status": "verification_failed"}), 200

    # Execute confirmation
    _handle_flutterwave_payment_confirmed(
        tx_ref=verification.get("tx_ref") or tx_ref,
        tx_id=str(tx_id),
        verification=verification,
    )

    return jsonify({"status": "success"}), 200


@payments_bp.route("/flutterwave/status", methods=["GET"])
@jwt_required(optional=True)
def flutterwave_status():
    """
    Check or proactively verify a Flutterwave transaction.
    Ensures immediate order confirmation upon redirect if the webhook is slightly delayed.
    """
    from app.services import flutterwave_service as _flw
    from app.models.escrow import EscrowTransaction

    tx_ref = request.args.get("tx_ref") or request.args.get("reference") or ""
    tx_id = request.args.get("transaction_id") or ""
    order_id_str = request.args.get("order_id") or ""

    escrow = None
    if tx_ref:
        escrow = EscrowTransaction.query.filter_by(transaction_number=tx_ref).first()
    if not escrow and order_id_str and order_id_str.isdigit():
        escrow = EscrowTransaction.query.filter_by(order_id=int(order_id_str)).first()

    if not escrow:
        return jsonify({"message": "Transaction not found"}), 404

    # If already released or in escrow, return immediately
    if escrow.status in ("IN_ESCROW", "RELEASED"):
        return jsonify({
            "orderId": str(escrow.order_id),
            "status": "COMPLETED",
            "transactionNumber": escrow.transaction_number,
        }), 200

    # If pending and transaction ID is available, proactively verify with Flutterwave
    if tx_id:
        try:
            verif = _flw.verify_transaction(tx_id)
            if verif.get("success") and verif.get("status") == "successful":
                _handle_flutterwave_payment_confirmed(
                    tx_ref=escrow.transaction_number,
                    tx_id=str(tx_id),
                    verification=verif,
                )
                return jsonify({
                    "orderId": str(escrow.order_id),
                    "status": "COMPLETED",
                    "transactionNumber": escrow.transaction_number,
                }), 200
        except Exception as exc:
            logger.warning("[FLW STATUS] Proactive verification error: %s", exc)

    return jsonify({
        "orderId": str(escrow.order_id),
        "status": escrow.status,
        "transactionNumber": escrow.transaction_number,
    }), 200


def _handle_flutterwave_payment_confirmed(tx_ref: str, tx_id: str, verification: dict):
    """
    Handle successful Flutterwave payment confirmation:
    Activates digital downloads, service bookings, or event tickets.
    """
    from app.models.escrow import EscrowTransaction, EscrowStatus
    from app.models.payment_link import PaymentLink
    from app.models.withdrawal import VendorBankAccount
    from app.routes.escrow import (
        _credit_vendor_ledger,
        _deliver_digital_products,
        _deliver_service_products,
        _deliver_event_tickets,
    )
    from app.models.communication import Notification

    try:
        escrows = EscrowTransaction.query.filter_by(transaction_number=tx_ref).all()
        if not escrows and tx_ref.startswith("FLW-"):
            # Try matching by order ID embedded in tx_ref: FLW-ORD-{id}-... or FLW-PL-{id}-...
            parts = tx_ref.split("-")
            if len(parts) >= 3 and parts[2].isdigit():
                oid = int(parts[2])
                escrow_single = EscrowTransaction.query.filter_by(order_id=oid).first()
                if escrow_single:
                    escrows = [escrow_single]

        if not escrows:
            logger.warning("[FLW CONFIRM] No escrow found for ref=%s tx_id=%s", tx_ref, tx_id)
            return

        for escrow in escrows:
            if escrow.status == EscrowStatus.RELEASED:
                continue

            order = escrow.order
            if not order:
                continue

            order.status = "PAID"
            order.payment_method = "FLUTTERWAVE"
            escrow.status = EscrowStatus.IN_ESCROW
            escrow.paid_at = _utcnow()
            escrow.payscrow_transaction_id = str(tx_id)
            db.session.flush()

            # 1. Event tickets
            is_event = _deliver_event_tickets(order, escrow)

            # 2. Digital products
            is_digital = False
            if not is_event:
                is_digital = _deliver_digital_products(order, escrow)

            # 3. Service bookings
            is_service = False
            if not is_event and not is_digital:
                is_service = _deliver_service_products(order, escrow)

            # 4. Pay Link orders
            if order.payment_link_id and not is_event and not is_digital and not is_service:
                link = db.session.get(PaymentLink, order.payment_link_id)
                link_ptype = getattr(link, "product_type", "service") or "service"
                if link_ptype in ("digital", "service"):
                    net_amount = float(escrow.amount) - float(escrow.fee_amount or 0)
                    escrow.status = EscrowStatus.RELEASED
                    escrow.released_at = _utcnow()
                    order.status = "COMPLETED"
                    if link:
                        link.status = "PAID"

                    _credit_vendor_ledger(
                        vendor_id=order.vendor_id,
                        amount=net_amount,
                        reference_id=escrow.transaction_number,
                        description=f"Flutterwave payout for Order #{order.id}",
                    )
                    db.session.add(Notification(
                        user_id=order.vendor_id,
                        title="Payment Received (Flutterwave)",
                        message=f"Order #{order.id} paid via Flutterwave. ₦{net_amount:,.2f} credited.",
                        type="ESCROW",
                        order_id=order.id,
                    ))
                    if order.buyer_id:
                        db.session.add(Notification(
                            user_id=order.buyer_id,
                            title="Payment Complete",
                            message=f"Your payment for Order #{order.id} is confirmed.",
                            type="ORDER",
                            order_id=order.id,
                        ))

                    # Email buyer with file/booking link from the Pay Link record
                    try:
                        from app.utils.email import send_siiqo_email
                        from app.models.user import User as _U
                        _buyer = db.session.get(_U, order.buyer_id) if order.buyer_id else None
                        _buyer_email = (
                            (_buyer.email if _buyer else None)
                            or getattr(order, "buyer_email", None)
                        )
                        if _buyer_email:
                            _first = (
                                (_buyer.first_name if _buyer else None)
                                or getattr(order, "buyer_name", None)
                                or "there"
                            )
                            _file_url = getattr(link, "file_url", None) if link else None
                            if link_ptype == "digital":
                                _link_html = (
                                    f'<p style="margin:8px 0;"><a href="{_file_url}" '
                                    f'style="color:#E0921C;word-break:break-all;">{_file_url}</a></p>'
                                    if _file_url
                                    else "<p>The vendor will send your download link shortly.</p>"
                                )
                                send_siiqo_email(
                                    to_email=_buyer_email,
                                    subject=f"Your Digital Download – Order #{order.id} | Siiqo",
                                    template_name="system_notice",
                                    first_name=_first,
                                    notice_text=(
                                        f"Your payment for Order #{order.id} is confirmed.<br><br>"
                                        f"Here is your download link:<br><br>{_link_html}<br>"
                                        "If you have any issues, please contact the seller via Siiqo chat."
                                    ),
                                )
                            else:  # service
                                _booking_url = getattr(link, "file_url", None) if link else None
                                _link_html = (
                                    f'<p style="margin:8px 0;"><a href="{_booking_url}" '
                                    f'style="color:#E0921C;word-break:break-all;">{_booking_url}</a></p>'
                                    if _booking_url
                                    else "<p>The vendor will reach out to schedule your service.</p>"
                                )
                                send_siiqo_email(
                                    to_email=_buyer_email,
                                    subject=f"Service Booking Confirmed – Order #{order.id} | Siiqo",
                                    template_name="system_notice",
                                    first_name=_first,
                                    notice_text=(
                                        f"Your payment for Order #{order.id} is confirmed.<br><br>"
                                        f"Use the link below to access your service:<br><br>{_link_html}<br>"
                                        "If you have any issues, please contact the seller via Siiqo chat."
                                    ),
                                )
                    except Exception as _email_err:
                        logger.warning("[FLW CONFIRM] Pay Link buyer email failed Order #%s: %s", order.id, _email_err)

            # Vendor payout check:
            # 1. If Flutterwave split was attached, settlement happens natively T+1 to vendor's subaccount.
            # 2. If split was NOT attached, the net amount was credited to their ledger above.
            #    If vendor has verified bank details, trigger automated instant Flutterwave Transfer payout:
            vendor_bank = VendorBankAccount.query.filter_by(vendor_id=order.vendor_id, is_default=True).first() or \
                          VendorBankAccount.query.filter_by(vendor_id=order.vendor_id).first()
            used_flw_split = bool(vendor_bank and vendor_bank.flw_subaccount_id)

            if not used_flw_split and escrow.status == EscrowStatus.RELEASED and vendor_bank and vendor_bank.bank_code and vendor_bank.account_number:
                net_amount = float(escrow.amount) - float(escrow.fee_amount or 0)
                try:
                    from decimal import Decimal
                    from app.routes.withdrawal import _debit_ledger
                    payout_ref = f"WD-FLW-{order.id}-{uuid.uuid4().hex[:6].upper()}"
                    flw_tx = _flw.transfer_to_vendor(
                        account_bank=vendor_bank.bank_code,
                        account_number=vendor_bank.account_number,
                        amount_ngn=net_amount,
                        reference=payout_ref,
                        narration=f"Siiqo payout for Order #{order.id}",
                    )
                    if flw_tx.get("success"):
                        _debit_ledger(
                            vendor_id=order.vendor_id,
                            amount=Decimal(str(net_amount)),
                            description=f"Auto-payout via Flutterwave Transfer for Order #{order.id}",
                            reference_id=payout_ref,
                        )
                        logger.info("[FLW CONFIRM] Payout transfer queued for vendor %s Order #%s: %s", order.vendor_id, order.id, payout_ref)
                except Exception as _tr_err:
                    logger.warning("[FLW CONFIRM] Automated Flutterwave payout transfer skipped: %s (funds remain in ledger)", _tr_err)

        db.session.commit()
        logger.info("[FLW CONFIRM] Orders confirmed for tx_ref=%s tx_id=%s", tx_ref, tx_id)

    except Exception as exc:
        db.session.rollback()
        logger.error("[FLW CONFIRM] Error confirming ref=%s: %s", tx_ref, exc)
