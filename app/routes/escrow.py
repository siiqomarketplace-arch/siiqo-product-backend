import logging
"""
escrow.py — Escrow lifecycle routes
Handles: initiate, status, webhook (PayScrow), release, dispute, admin actions
"""
import uuid
import hmac
import hashlib
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


# ---------------------------------------------------------------------------
# POST /escrow/initiate
# ---------------------------------------------------------------------------

@escrow_bp.route('/initiate', methods=['POST'])
@jwt_required()
def initiate_escrow():
    user_id = get_jwt_identity()
    data = request.get_json() or {}
    order_id = data.get('orderId') or data.get('order_id')

    order = db.session.get(Order, order_id)
    if not order:
        return jsonify({"message": "Order not found"}), 404
    if order.buyer_id != int(user_id):
        return jsonify({"message": "Unauthorized"}), 403

    escrow_txn = EscrowTransaction.query.filter_by(order_id=order.id).first()
    if not escrow_txn:
        vendor_bank = VendorBankAccount.query.filter_by(vendor_id=order.vendor_id, is_default=True).first()
        if not vendor_bank:
            return jsonify({"message": "Vendor has not set up a receiving bank account."}), 400

        txn_number = f"ESC-{uuid.uuid4().hex[:12].upper()}"
        
        # We waive the 12% fee for beta testing, allocating 100% of funds to vendor settlement
        fee_amount = 0.0
        vendor_payout = float(order.total_amount)

        # Call PayScrow API
        payscrow_key = os.environ.get('PAYSCROW_API_KEY', '')
        base_url = "https://api.payscrow.net" if payscrow_key else "https://api.payscrow.dev"
        headers = {
            "BrokerApiKey": payscrow_key,
            "Content-Type": "application/json"
        }
        
        payload = {
            "transactionReference": txn_number,
            "merchantEmailAddress": order.vendor.email if order.vendor else "vendor@siiqo.com",
            "merchantName": order.vendor.first_name if order.vendor else "Siiqo Vendor",
            "customerEmailAddress": order.buyer.email if order.buyer else "buyer@siiqo.com",
            "customerName": order.buyer.first_name if order.buyer else "Siiqo Buyer",
            "currencyCode": "NGN",
            "merchantChargePercentage": 0,  # Buyer pays escrow charges if any, or 0
            "webhookNotificationUrl": os.environ.get(
                'PAYSCROW_WEBHOOK_URL',
                "https://api.siiqo.com/api/escrow/webhook"
            ),
            "items": [
                {
                    "name": f"Siiqo Order #{order.id}",
                    "quantity": 1,
                    "price": float(order.total_amount)
                }
            ],
            "settlementAccounts": [
                {
                    "bankCode": vendor_bank.bank_code,
                    "accountNumber": vendor_bank.account_number,
                    "accountName": vendor_bank.account_name,
                    "amount": vendor_payout
                }
            ]
        }
        
        try:
            resp = requests.post(f"{base_url}/api/v3/marketplace/transactions/start", json=payload, headers=headers)
            resp_data = resp.json()
            if not resp_data.get('success'):
                return jsonify({"message": "Escrow init failed", "errors": resp_data.get('errors')}), 400
            
            payment_link = resp_data['data']['paymentLink']
            payscrow_id = resp_data['data'].get('transactionId')          # GUID — used for applycode
            payscrow_txn_number = resp_data['data'].get('transactionNumber')  # MKT-XXXXX — used for dispute/status
        except Exception as e:
            logging.error(f"Payscrow API error: {e}")
            return jsonify({"message": "Could not connect to Escrow provider"}), 500

        escrow_txn = EscrowTransaction(
            order_id=order.id,
            transaction_number=txn_number,
            status=EscrowStatus.PENDING_PAYMENT,
            amount=order.total_amount,
            fee_percent=0.00,
            fee_amount=fee_amount,
            payment_link=payment_link,
            payscrow_transaction_id=str(payscrow_id) if payscrow_id else None,
            payscrow_ref=str(payscrow_txn_number) if payscrow_txn_number else None,  # MKT-XXXXX
        )
        db.session.add(escrow_txn)
        db.session.commit()

    return jsonify({
        "success": True,
        "paymentLink": escrow_txn.payment_link,
        "transactionNumber": escrow_txn.transaction_number,
        "amount": str(escrow_txn.amount),
        "status": escrow_txn.status,
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
# POST /escrow/webhook  — PayScrow payment confirmation
# ---------------------------------------------------------------------------

@escrow_bp.route('/webhook', methods=['POST'])
def payscrow_webhook():
    """
    Receives payment confirmation from PayScrow.
    Verifies HMAC signature, then advances escrow status to IN_ESCROW.
    """
    payload = request.get_data()
    sig_header = request.headers.get('X-PayScrow-Signature', '')
    secret = os.environ.get('PAYSCROW_WEBHOOK_SECRET', '')

    if secret:
        expected = hmac.new(secret.encode(), payload, hashlib.sha256).hexdigest()
        if not hmac.compare_digest(expected, sig_header):
            return jsonify({"message": "Invalid signature"}), 401

    data = request.get_json(force=True) or {}
    
    # PayScrow v3.0 Webhook structure
    txn_ref = data.get('externalReference') or data.get('transactionNumber')
    payment_status = data.get('paymentStatus')
    escrow_code = data.get('escrowCode')
    payscrow_transaction_id = data.get('transactionId')

    if payment_status == 'Paid' and txn_ref:
        escrow = EscrowTransaction.query.filter_by(transaction_number=txn_ref).first()
        if escrow and escrow.status == EscrowStatus.PENDING_PAYMENT:
            escrow.status = EscrowStatus.IN_ESCROW
            escrow.paid_at = _utcnow()
            escrow.escrow_code = escrow_code
            if payscrow_transaction_id:
                escrow.payscrow_transaction_id = payscrow_transaction_id

            order = escrow.order
            if order:
                order.status = 'PAID'
                # Notify buyer
                db.session.add(Notification(
                    user_id=order.buyer_id,
                    title="Payment Confirmed",
                    message=f"Your payment for Order #{order.id} is confirmed and held in escrow.",
                    type="ESCROW",
                    order_id=order.id,
                ))
                # Notify vendor
                db.session.add(Notification(
                    user_id=order.vendor_id,
                    title="Payment Received in Escrow",
                    message=f"Payment for Order #{order.id} is secured in escrow. Please ship the order.",
                    type="ESCROW",
                    order_id=order.id,
                ))

            db.session.commit()

            # Send Email Notifications AFTER commit
            if order:
                from app.utils.email import send_siiqo_email
                # Email Buyer
                if order.buyer:
                    try:
                        send_siiqo_email(
                            to_email=order.buyer.email,
                            subject="Siiqo Payment Secured",
                            template_name="payment_escrow",
                            first_name=order.buyer.first_name or "Buyer",
                            order_id=order.id,
                            amount=f"₦{escrow.amount:,.2f}"
                        )
                        # And order confirmation
                        send_siiqo_email(
                            to_email=order.buyer.email,
                            subject="Siiqo Order Confirmation",
                            template_name="order_confirmation",
                            first_name=order.buyer.first_name or "Buyer",
                            order_id=order.id
                        )
                    except Exception:
                        pass
                
                # Email Vendor
                if order.vendor:
                    try:
                        send_siiqo_email(
                            to_email=order.vendor.email,
                            subject="New Siiqo Order Received!",
                            template_name="order_received_vendor",
                            vendor_name=order.vendor.first_name or "Vendor",
                            order_id=order.id
                        )
                    except Exception:
                        pass

    return jsonify({"received": True}), 200


# ---------------------------------------------------------------------------
# POST /escrow/release  — Buyer confirms delivery → release funds
# ---------------------------------------------------------------------------

@escrow_bp.route('/release', methods=['POST'])
@jwt_required()
def release_escrow():
    user_id = get_jwt_identity()
    data = request.get_json() or {}
    txn_number = data.get('transactionId') or data.get('transaction_number')
    order_id = data.get('order_id')

    if txn_number:
        escrow = db.session.query(EscrowTransaction).filter_by(transaction_number=txn_number).with_for_update().first()
    elif order_id:
        escrow = db.session.query(EscrowTransaction).filter_by(order_id=order_id).with_for_update().first()
    else:
        return jsonify({"message": "transactionId or order_id required"}), 400

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
                db.session.commit()
                return jsonify({
                    "success": True,
                    "message": "Order marked as completed.",
                }), 200
        return jsonify({"message": "Transaction not found"}), 404

    order = escrow.order
    if order.buyer_id != int(user_id):
        return jsonify({"message": "Only the buyer can release funds"}), 403

    if escrow.status not in [EscrowStatus.IN_ESCROW, EscrowStatus.DELIVERED, EscrowStatus.SHIPPED]:
        return jsonify({"message": f"Cannot release funds at status: {escrow.status}"}), 400

    if not escrow.payscrow_transaction_id or not escrow.escrow_code:
        return jsonify({"message": "Missing escrow code or Payscrow ID to release funds."}), 400

    # Call PayScrow API to apply code and release funds
    payscrow_key = os.environ.get('PAYSCROW_API_KEY', '')
    base_url = "https://api.payscrow.net" if payscrow_key else "https://api.payscrow.dev"
    headers = {
        "BrokerApiKey": payscrow_key,
        "Content-Type": "application/json"
    }
    payload = {
        "transactionId": escrow.payscrow_transaction_id,
        "code": escrow.escrow_code
    }
    
    try:
        resp = requests.post(f"{base_url}/api/v3/escrow/escrowtransactions/applycode", json=payload, headers=headers)
        resp_data = resp.json()
        if not resp_data.get('success'):
            return jsonify({"message": "Failed to release escrow via Payscrow", "errors": resp_data.get('errors')}), 400
    except Exception as e:
        logging.error(f"Payscrow release API error: {e}")
        return jsonify({"message": "Could not connect to Escrow provider"}), 500

    escrow.status = EscrowStatus.RELEASED
    escrow.released_at = _utcnow()
    order.status = 'COMPLETED'

    # Credit vendor ledger (net of fee)
    net_amount = float(escrow.amount) - float(escrow.fee_amount or 0)
    _credit_vendor_ledger(
        vendor_id=order.vendor_id,
        amount=net_amount,
        reference_id=escrow.transaction_number,
        description=f"Payout for Order #{order.id}",
    )
    
    # Since Payscrow paid them directly to their bank, write a matching DEBIT so balance remains accurate
    from app.models.finance import Ledger as L
    from sqlalchemy import func
    credits = db.session.query(func.sum(L.amount)).filter_by(vendor_id=order.vendor_id, transaction_type='CREDIT').scalar() or 0
    debits = db.session.query(func.sum(L.amount)).filter_by(vendor_id=order.vendor_id, transaction_type='DEBIT').scalar() or 0
    balance_after_debit = float(credits) - float(debits) - net_amount
    
    db.session.add(L(
        vendor_id=order.vendor_id,
        transaction_type='DEBIT',
        amount=net_amount,
        description=f"Auto-Settled via Payscrow (Order #{order.id})",
        reference_id=escrow.transaction_number + "-SETTLED",
        balance_after=balance_after_debit,
    ))

    # Create Receipt
    db.session.add(Receipt(order_id=order.id))

    # Notify vendor
    db.session.add(Notification(
        user_id=order.vendor_id,
        title="Funds Released",
        message=f"₦{net_amount:,.2f} has been credited to your ledger for Order #{order.id}.",
        type="ESCROW",
        order_id=order.id,
    ))

    # Notify buyer
    db.session.add(Notification(
        user_id=order.buyer_id,
        title="Order Complete",
        message=f"Order #{order.id} is complete. Thank you for shopping on Siiqo!",
        type="ORDER",
        order_id=order.id,
    ))

    db.session.commit()

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
    payscrow_key = os.environ.get('PAYSCROW_API_KEY', '')
    base_url = "https://api.payscrow.net" if payscrow_key else "https://api.payscrow.dev"
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

    return jsonify({
        "success": True,
        "disputeId": escrow.dispute_id,
        "message": "Dispute raised. Funds are frozen. Our team will review within 48 hours.",
    }), 200
