"""
escrow.py — Escrow lifecycle routes
Handles: initiate, status, Paystack webhook, release, dispute, admin actions

Payment provider split:
  - Marketplace checkout  → Paystack  (ACTIVE_ESCROW_PROVIDER=paystack)
  - Payment Links (/pay)  → Payscrow  (payment_links.py, unchanged)
  - Subscriptions         → Paystack  (bridge.py, unchanged)
"""
import logging
import uuid
import os
from datetime import datetime, timezone

from flask import Blueprint, request, jsonify
from flask_jwt_extended import jwt_required, get_jwt_identity

from app.extensions import db
from app.models.order import Order
from app.models.escrow import EscrowTransaction, EscrowStatus
from app.models.finance import Ledger, Receipt
from app.models.communication import Notification
from app.models.withdrawal import PODPayment, VendorBankAccount
import requests

escrow_bp = Blueprint('escrow', __name__)


def _utcnow():
    return datetime.now(timezone.utc)


def _payscrow_env():
    """Return (api_key, base_url) for Payscrow — still used by Payment Links."""
    key = os.environ.get('PAYSCROW_API_KEY', '')
    base_url = os.environ.get('PAYSCROW_BASE_URL')
    if not base_url:
        is_sandbox = (
            not key
            or key.startswith('ps_9')
            or os.environ.get('PAYSCROW_ENV', '').lower() == 'sandbox'
        )
        base_url = "https://api.payscrow.dev" if is_sandbox else "https://api.payscrow.net"
    return key, base_url


def _active_provider() -> str:
    """Return the currently-configured payment provider name."""
    return os.environ.get("ACTIVE_ESCROW_PROVIDER", "payscrow").lower()


def _credit_vendor_ledger(vendor_id: int, amount: float, reference_id: str, description: str):
    """Write a CREDIT entry to the vendor's ledger."""
    # Calculate running balance
    from sqlalchemy import func
    from app.models.finance import Ledger as L
    credits = db.session.query(func.sum(L.amount)).filter_by(
        vendor_id=vendor_id, transaction_type='CREDIT'
    ).scalar() or 0
    debits = db.session.query(func.sum(L.amount)).filter_by(
        vendor_id=vendor_id, transaction_type='DEBIT'
    ).scalar() or 0
    balance_after = float(credits) - float(debits) + amount

    db.session.add(Ledger(
        vendor_id=vendor_id,
        transaction_type='CREDIT',
        amount=amount,
        description=description,
        reference_id=reference_id,
        balance_after=balance_after,
    ))


from app.services.escrow import get_escrow_provider

# ---------------------------------------------------------------------------
# POST /escrow/initiate
# ---------------------------------------------------------------------------

@escrow_bp.route('/initiate', methods=['POST'])
@jwt_required()
def initiate_escrow():
    user_id = get_jwt_identity()
    data = request.get_json() or {}
    order_id_param = data.get('orderId') or data.get('order_id')

    # Handle comma-separated list of order IDs for Unified Payment
    order_ids = [int(oid) for oid in str(order_id_param).split(',') if oid.isdigit()]
    if not order_ids:
        return jsonify({"message": "Order not found"}), 404

    orders = Order.query.filter(Order.id.in_(order_ids)).all()
    if not orders:
        return jsonify({"message": "Orders not found"}), 404

    for order in orders:
        if order.buyer_id != int(user_id):
            return jsonify({"message": "Unauthorized"}), 403

    # Validate that all vendors have bank accounts for Payscrow split payouts
    from app.models.withdrawal import VendorBankAccount
    for order in orders:
        bank_acc = VendorBankAccount.query.filter_by(vendor_id=order.vendor_id, is_default=True).first()
        if not bank_acc:
            bank_acc = VendorBankAccount.query.filter_by(vendor_id=order.vendor_id).first()
        if not bank_acc:
            sf = order.vendor.storefront if order.vendor else None
            if not (sf and sf.bank_code and sf.account_number):
                v_name = order.vendor.full_name if order.vendor else f"ID {order.vendor_id}"
                return jsonify({"message": f"Escrow payment is unavailable because the vendor '{v_name}' has not configured their payout bank details. Please contact the vendor to update their details or choose a different payment method."}), 400

    escrow_txns = EscrowTransaction.query.filter(EscrowTransaction.order_id.in_([o.id for o in orders])).all()
    existing_txn_number = next((e.transaction_number for e in escrow_txns if e.payment_link), None)

    # Delegate logic to Escrow Service Provider (Unified Payment)
    provider = get_escrow_provider()
    result = provider.initiate_transaction(orders, existing_txn_number)
    
    if not result.get("success"):
        return jsonify({"message": result.get("error_message") or "Escrow init failed"}), 400

    txn_map = {e.order_id: e for e in escrow_txns}
    
    for order in orders:
        escrow_txn = txn_map.get(order.id)
        if not escrow_txn:
            # fee_amount for an individual order in a master transaction
            # the result['fee_amount'] is total, so we calculate proportional
            individual_fee = round(float(order.total_amount) * 0.06, 2)
            escrow_txn = EscrowTransaction(
                order_id=order.id,
                transaction_number=result['transaction_number'],
                status=EscrowStatus.PENDING_PAYMENT,
                amount=float(order.total_amount),
                fee_percent=6.00,
                fee_amount=individual_fee,
                payment_link=result['payment_link'],
                payscrow_transaction_id=result['provider_transaction_id'],
                payscrow_ref=result['provider_reference'],
            )
            db.session.add(escrow_txn)
        else:
            escrow_txn.transaction_number = result['transaction_number']
            escrow_txn.payment_link = result['payment_link']
            escrow_txn.payscrow_transaction_id = result['provider_transaction_id']
            escrow_txn.payscrow_ref = result['provider_reference']
            
    db.session.commit()

    return jsonify({
        "success": True,
        "paymentLink": result.get('payment_link'),
        "transactionNumber": result.get('transaction_number'),
        "amount": str(result.get('amount', 0)),
        "status": EscrowStatus.PENDING_PAYMENT,
    }), 200


# ---------------------------------------------------------------------------
# GET /escrow/status
# ---------------------------------------------------------------------------

@escrow_bp.route('/status', methods=['GET'])
@jwt_required()
def escrow_status():
    txn_number = request.args.get('txn')
    order_id = request.args.get('order_id')

    if txn_number:
        escrow = EscrowTransaction.query.filter_by(transaction_number=txn_number).first()
    elif order_id:
        escrow = EscrowTransaction.query.filter_by(order_id=order_id).first()
    else:
        return jsonify({"message": "txn or order_id required"}), 400

    if not escrow:
        return jsonify({"message": "Transaction not found"}), 404

    return jsonify(escrow.to_dict()), 200


# ---------------------------------------------------------------------------
# POST /escrow/webhook  — Paystack payment confirmation (marketplace orders)
#
# NOTE: Payscrow webhook for Payment Links is handled by payment_links.py
#       and still posts to /api/escrow/webhook (payscrow_webhook below).
#       We keep BOTH handlers under different sub-paths and route them by
#       the env var so existing Payscrow payment links keep working.
# ---------------------------------------------------------------------------

@escrow_bp.route('/webhook', methods=['POST'])
def payscrow_webhook():
    """
    Legacy Payscrow webhook — still active for Payment Link orders.
    Paystack marketplace orders are handled in bridge.py /payments/webhook.
    """
    payload = request.get_data()
    data = request.get_json(force=True) or {}

    txn_ref = data.get('externalReference') or data.get('transactionNumber')
    payment_status = data.get('paymentStatus')
    escrow_code = data.get('escrowCode')
    payscrow_transaction_id = data.get('transactionId')

    logging.info(
        f"PAYSCROW WEBHOOK: txn_ref={txn_ref}, "
        f"payment_status={payment_status}, escrow_code={escrow_code}"
    )

    if payment_status and str(payment_status).lower() == 'paid' and txn_ref:
        escrows = EscrowTransaction.query.filter_by(transaction_number=txn_ref).all()
        processed_orders = []

        for escrow in escrows:
            if escrow.status == EscrowStatus.PENDING_PAYMENT:
                escrow.status = EscrowStatus.IN_ESCROW
                escrow.paid_at = _utcnow()
                escrow.escrow_code = escrow_code
                if payscrow_transaction_id:
                    escrow.payscrow_transaction_id = payscrow_transaction_id

                order = escrow.order
                if order:
                    order.status = 'PAID'

                    if order.payment_link_id:
                        from app.models.payment_link import PaymentLink
                        link = db.session.get(PaymentLink, order.payment_link_id)
                        if link and link.link_type == 'INVOICE':
                            link.status = 'PAID'

                    from app.models.escrow import LogisticsAssignment
                    assignment = LogisticsAssignment.query.filter_by(order_id=order.id).first()
                    if assignment and assignment.status == 'PENDING':
                        assignment.status = 'ASSIGNED'
                        assignment.assigned_at = _utcnow()
                        db.session.add(Notification(
                            user_id=assignment.partner_id,
                            title="New Delivery Assignment",
                            message=(
                                f"You have been assigned a new delivery for Order #{order.id}. "
                                f"Delivery fee: ₦{assignment.delivery_fee:,.2f}."
                            ),
                            type="DELIVERY",
                            order_id=order.id,
                        ))

                    processed_orders.append((escrow, order))
                    db.session.add(Notification(
                        user_id=order.buyer_id,
                        title="Payment Confirmed",
                        message=f"Your payment for Order #{order.id} is confirmed and held in escrow.",
                        type="ORDER",
                        order_id=order.id,
                    ))
                    db.session.add(Notification(
                        user_id=order.vendor_id,
                        title="Payment Received in Escrow",
                        message=f"Payment for Order #{order.id} is secured. Please ship the order.",
                        type="ESCROW",
                        order_id=order.id,
                    ))

        db.session.commit()

        from app.utils.email import send_siiqo_email
        from app.models.user import User

        for escrow, order in processed_orders:
            buyer = db.session.get(User, order.buyer_id)
            if buyer:
                try:
                    send_siiqo_email(
                        to_email=buyer.email,
                        subject=f"Order Confirmation #{order.id} - Siiqo",
                        template_name="order_confirmation",
                        first_name=buyer.first_name or "there",
                        order_id=order.id,
                        payment_method="ESCROW",
                    )
                except Exception as e:
                    logging.warning(f"[EMAIL] buyer confirm email failed Order #{order.id}: {e}")

            vendor = db.session.get(User, order.vendor_id)
            if vendor:
                try:
                    send_siiqo_email(
                        to_email=vendor.email,
                        subject="New Order - Siiqo",
                        template_name="order_received_vendor",
                        first_name=vendor.first_name or "Vendor",
                        order_id=order.id,
                        total_amount=f"₦{float(order.total_amount):,.2f}",
                        payment_method="ESCROW",
                    )
                except Exception as e:
                    logging.warning(f"[EMAIL] vendor email failed Order #{order.id}: {e}")

    return jsonify({"received": True}), 200


# ---------------------------------------------------------------------------
# POST /escrow/release  — Buyer confirms delivery → release funds to vendor
#
# Paystack flow:  funds already sit in Siiqo's Paystack balance.
#                 We call Paystack /transfer to push vendor's net share.
# Payscrow flow:  legacy applycode path (Payment Links only).
# ---------------------------------------------------------------------------

@escrow_bp.route('/release', methods=['POST'])
@jwt_required()
def release_escrow():
    user_id = get_jwt_identity()
    data = request.get_json() or {}
    txn_number = data.get('transactionId') or data.get('transaction_number')
    order_id = data.get('order_id')

    if txn_number:
        escrow = db.session.query(EscrowTransaction).filter_by(
            transaction_number=txn_number
        ).with_for_update().first()
    elif order_id:
        escrow = db.session.query(EscrowTransaction).filter_by(
            order_id=order_id
        ).with_for_update().first()
    else:
        return jsonify({"message": "transactionId or order_id required"}), 400

    # ── POD fall-through ─────────────────────────────────────────────────────
    if not escrow:
        if order_id:
            pod = PODPayment.query.filter_by(order_id=order_id).first()
            if pod:
                order = pod.order
                if order.buyer_id != int(user_id):
                    return jsonify({"message": "Only the buyer can release funds"}), 403
                order.status = 'COMPLETED'
                pod.payment_status = 'collected'
                db.session.add(Notification(
                    user_id=order.vendor_id,
                    title="Order Complete",
                    message=f"Buyer confirmed receipt for POD Order #{order.id}.",
                    type="ORDER",
                    order_id=order.id,
                ))
                try:
                    from app.services.referral_service import check_and_reward_referral_on_order_complete
                    check_and_reward_referral_on_order_complete(order)
                except Exception as ex:
                    logging.error(f"[REFERRAL ERR] POD confirm referral reward failed: {ex}")
                db.session.commit()
                return jsonify({"success": True, "message": "Order marked as completed."}), 200
        return jsonify({"message": "Transaction not found"}), 404

    order = escrow.order
    if order.buyer_id != int(user_id):
        return jsonify({"message": "Only the buyer can release funds"}), 403

    # ── Fallback verify if still PENDING_PAYMENT ─────────────────────────────
    if escrow.status == EscrowStatus.PENDING_PAYMENT:
        provider = _active_provider()
        if provider == "paystack":
            from app.services.escrow.paystack_provider import PaystackProvider
            result = PaystackProvider().verify_transaction(escrow.transaction_number)
            if result.get("success"):
                escrow.status = EscrowStatus.IN_ESCROW
                escrow.paid_at = _utcnow()
                db.session.commit()
        else:
            # Legacy Payscrow verify
            payscrow_key, base_url = _payscrow_env()
            headers = {"BrokerApiKey": payscrow_key}
            try:
                resp = requests.get(
                    f"{base_url}/api/v3/marketplace/transactions/"
                    f"{escrow.transaction_number}/status",
                    headers=headers,
                )
                if resp.status_code == 200:
                    status_data = resp.json()
                    p_status = str(status_data.get('paymentStatus', '')).lower()
                    if p_status in ['paid', 'completed', 'pendingsettlement']:
                        escrow.status = EscrowStatus.IN_ESCROW
                        escrow.paid_at = _utcnow()
                        if status_data.get('escrowCode'):
                            escrow.escrow_code = status_data.get('escrowCode')
                        if status_data.get('transactionId'):
                            escrow.payscrow_transaction_id = status_data.get('transactionId')
                        db.session.commit()
            except Exception as e:
                logging.error(f"Fallback status check failed: {e}")

    if escrow.status not in [
        EscrowStatus.IN_ESCROW, EscrowStatus.DELIVERED, EscrowStatus.SHIPPED
    ]:
        return jsonify({
            "message": f"Cannot release funds at status: {escrow.status}"
        }), 400

    # ── Provider-specific fund release ───────────────────────────────────────
    provider = _active_provider()

    if provider == "paystack":
        # Siiqo holds the full payment in its Paystack balance.
        # Push vendor's net share via Paystack Transfers API.
        net_amount = float(escrow.amount) - float(escrow.fee_amount or 0)

        bank_acc = VendorBankAccount.query.filter_by(
            vendor_id=order.vendor_id, is_default=True
        ).first()
        if not bank_acc:
            bank_acc = VendorBankAccount.query.filter_by(
                vendor_id=order.vendor_id
            ).first()

        if bank_acc and bank_acc.recipient_code:
            from app.services.escrow.paystack_provider import paystack_transfer_to_vendor
            transfer_result = paystack_transfer_to_vendor(
                recipient_code=bank_acc.recipient_code,
                amount_ngn=net_amount,
                reference=f"PAYOUT-{order.id}-{uuid.uuid4().hex[:6].upper()}",
                reason=f"Siiqo payout for Order #{order.id}",
            )
            if not transfer_result.get("success"):
                # Log but don't block — ledger credit still happens so vendor
                # can manually request withdrawal if the transfer errors.
                logging.error(
                    f"[PAYSTACK TRANSFER] Failed for Order #{order.id}: "
                    f"{transfer_result.get('error_message')}"
                )
        else:
            # Vendor hasn't added a bank account yet — credit ledger only.
            # They can withdraw manually once bank details are added.
            logging.warning(
                f"[RELEASE] Vendor {order.vendor_id} has no recipient_code. "
                "Crediting ledger only — no Paystack transfer."
            )
    else:
        # ── Legacy Payscrow applycode (Payment Links) ─────────────────────
        if not escrow.payscrow_transaction_id:
            return jsonify({
                "message": "Missing payment transaction ID. Cannot verify payment."
            }), 400

        user_submitted_code = (
            data.get('escrowCode') or data.get('escrow_code') or ''
        ).strip()
        raw_code = (
            user_submitted_code
            if user_submitted_code
            else (str(escrow.escrow_code).strip() if escrow.escrow_code else "")
        )
        code_is_real = raw_code.isdigit() and 4 <= len(raw_code) <= 10

        if code_is_real:
            payscrow_key, base_url = _payscrow_env()
            headers = {
                "BrokerApiKey": payscrow_key,
                "Content-Type": "application/json",
            }
            try:
                resp = requests.post(
                    f"{base_url}/api/v3/escrow/escrowtransactions/applycode",
                    json={"transactionId": escrow.payscrow_transaction_id, "code": raw_code},
                    headers=headers,
                    timeout=15,
                )
                resp_data = resp.json()
                if not resp_data.get('success'):
                    logging.warning(
                        f"Payscrow applycode non-success for "
                        f"{escrow.transaction_number}: {resp.text}"
                    )
                    is_sandbox = (
                        not payscrow_key
                        or payscrow_key.startswith('ps_9')
                        or os.environ.get('PAYSCROW_ENV', '').lower() == 'sandbox'
                    )
                    if not is_sandbox:
                        return jsonify({
                            "success": False,
                            "message": f"Payscrow release failed: "
                                       f"{resp_data.get('message', 'Invalid release code')}",
                        }), 400
            except Exception as e:
                logging.warning(
                    f"Payscrow applycode unreachable for "
                    f"{escrow.transaction_number}: {e} — releasing internally"
                )
        else:
            logging.info(
                f"Escrow code '{raw_code[:40]}' is not a numeric release code "
                "— releasing internally."
            )

    # ── Common post-release logic (both providers) ────────────────────────────
    net_amount = float(escrow.amount) - float(escrow.fee_amount or 0)

    escrow.status = EscrowStatus.RELEASED
    escrow.released_at = _utcnow()
    order.status = 'COMPLETED'

    try:
        from app.services.referral_service import check_and_reward_referral_on_order_complete
        check_and_reward_referral_on_order_complete(order)
    except Exception as ex:
        logging.error(f"[REFERRAL ERR] Escrow release referral reward failed: {ex}")

    _credit_vendor_ledger(
        vendor_id=order.vendor_id,
        amount=net_amount,
        reference_id=escrow.transaction_number,
        description=f"Payout for Order #{order.id}",
    )

    db.session.add(Receipt(order_id=order.id))

    db.session.add(Notification(
        user_id=order.vendor_id,
        title="Funds Released",
        message=f"₦{net_amount:,.2f} has been credited to your account for Order #{order.id}.",
        type="ESCROW",
        order_id=order.id,
    ))
    db.session.add(Notification(
        user_id=order.buyer_id,
        title="Order Complete",
        message=f"Order #{order.id} is complete. Thank you for shopping on Siiqo!",
        type="ORDER",
        order_id=order.id,
    ))

    db.session.commit()

    try:
        from app.services.trust import recalculate_vendor_trust
        recalculate_vendor_trust(order.vendor_id, reason="Escrow Released")
    except Exception as e:
        logging.error(f"[TRUST ERROR] Failed to recalculate trust on escrow release: {e}")

    from app.utils.email import send_siiqo_email
    from app.models.user import User

    vendor = db.session.get(User, order.vendor_id)
    if vendor and vendor.email:
        try:
            send_siiqo_email(
                to_email=vendor.email,
                subject="Siiqo - Payout Released",
                template_name="system_notice",
                first_name=vendor.first_name or "Vendor",
                notice_text=(
                    f"Congratulations! Payout of ₦{net_amount:,.2f} has been released "
                    f"to your account for Order #{order.id}."
                ),
            )
        except Exception as e:
            logging.warning(f"[EMAIL WARN] payout release email failed: {e}")

    buyer = db.session.get(User, order.buyer_id)
    if buyer and buyer.email:
        try:
            send_siiqo_email(
                to_email=buyer.email,
                subject="Siiqo - Order Completed",
                template_name="system_notice",
                first_name=buyer.first_name or "Buyer",
                notice_text=(
                    f"Thank you! Order #{order.id} is now complete. "
                    "Funds have been released to the vendor."
                ),
            )
        except Exception as e:
            logging.warning(f"[EMAIL WARN] order completed email failed: {e}")

    return jsonify({
        "success": True,
        "message": "Funds released to vendor successfully.",
        "net_amount": str(net_amount),
    }), 200


# ---------------------------------------------------------------------------
# POST /escrow/dispute
# ---------------------------------------------------------------------------

@escrow_bp.route('/dispute', methods=['POST'])
@jwt_required()
def raise_dispute():
    user_id = get_jwt_identity()
    data = request.get_json() or {}
    txn_number = data.get('transactionNumber') or data.get('transaction_number')
    order_id = data.get('order_id')
    reason = data.get('reason', '')

    if txn_number:
        escrow = EscrowTransaction.query.filter_by(transaction_number=txn_number).first()
    elif order_id:
        escrow = EscrowTransaction.query.filter_by(order_id=order_id).first()
    else:
        return jsonify({"message": "transactionNumber or order_id required"}), 400

    if not escrow:
        # Check if this is a POD order
        pod = None
        if order_id:
            pod = PODPayment.query.filter_by(order_id=order_id).first()
            
        if not pod:
            return jsonify({"message": "Transaction not found"}), 404
            
        # Handle POD Dispute
        order = pod.order
        if order.buyer_id != int(user_id) and order.vendor_id != int(user_id):
            return jsonify({"message": "Unauthorized"}), 403
            
        dispute_id = f"DISP-{uuid.uuid4().hex[:8].upper()}"
        pod.vendor_notes = f"[DISPUTED: {dispute_id} - {reason}] " + (pod.vendor_notes or "")
        
        # Notify both parties
        for uid in [order.buyer_id, order.vendor_id]:
            db.session.add(Notification(
                user_id=uid,
                title="POD Dispute Raised",
                message=f"A dispute has been raised on Pay-on-Delivery Order #{order.id}.",
                type="ORDER",
                order_id=order.id,
            ))
            
        db.session.commit()
        return jsonify({
            "success": True,
            "disputeId": dispute_id,
            "message": "Dispute raised. Our team will review within 48 hours."
        }), 200

    order = escrow.order
    if order.buyer_id != int(user_id) and order.vendor_id != int(user_id):
        return jsonify({"message": "Unauthorized"}), 403

    if escrow.status == EscrowStatus.DISPUTED:
        return jsonify({"message": "A dispute is already open on this transaction."}), 400

    if escrow.status not in [EscrowStatus.IN_ESCROW, EscrowStatus.DELIVERED, EscrowStatus.SHIPPED]:
        return jsonify({"message": f"Cannot raise a dispute at status: {escrow.status}"}), 400

    # Determine who is disputing: buyer = 'customer', vendor = 'merchant'
    requested_by = "customer" if order.buyer_id == int(user_id) else "merchant"

    # Notify Payscrow to officially freeze funds on their end
    payscrow_key, base_url = _payscrow_env()
    headers = {
        "BrokerApiKey": payscrow_key,
        "Content-Type": "application/json"
    }

    if escrow.payscrow_ref and payscrow_key:
        try:
            resp = requests.post(
                f"{base_url}/api/v3/marketplace/transactions/{escrow.payscrow_ref}/broker/raise-dispute",
                json={"requestedBy": requested_by, "complaint": reason or "No reason provided."},
                headers=headers,
                timeout=10
            )
            if not resp.json().get('success'):
                logging.warning(f"Payscrow dispute API returned non-success: {resp.text}")
        except Exception as e:
            logging.error(f"Payscrow dispute API error: {e}")
            # We still mark it locally — don't block the user if network issue

    dispute_id = f"DISP-{uuid.uuid4().hex[:8].upper()}"
    escrow.status = EscrowStatus.DISPUTED
    escrow.dispute_id = dispute_id
    escrow.dispute_reason = reason

    # Also update order status so vendor sees DISPUTED in their dashboard
    order.status = 'DISPUTED'

    # Notify both parties
    for uid in [order.buyer_id, order.vendor_id]:
        db.session.add(Notification(
            user_id=uid,
            title="Dispute Raised",
            message=f"A dispute has been raised on Order #{order.id}. Funds are frozen pending resolution.",
            type="ESCROW",
            order_id=order.id,
        ))

    db.session.commit()

    # Trigger trust score recalculation instantly
    try:
        from app.services.trust import recalculate_vendor_trust
        recalculate_vendor_trust(order.vendor_id, reason="Dispute Raised")
    except Exception as e:
        logging.error(f"[TRUST ERROR] Failed to recalculate trust on dispute raise: {e}")

    return jsonify({
        "success": True,
        "disputeId": escrow.dispute_id,
        "message": "Dispute raised. Funds are frozen. Our team will review within 48 hours.",
    }), 200
