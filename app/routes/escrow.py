"""
escrow.py — Escrow lifecycle routes
Handles: initiate, status, Paystack webhook, Flutterwave webhook, release, dispute, admin actions

Payment provider split:
  - Marketplace checkout (physical) → Paystack  (ACTIVE_ESCROW_PROVIDER=paystack)
  - Marketplace checkout (digital, service, event) → Flutterwave or Paystack (card)
  - Payment Links (/pay)  → Payscrow  (payment_links.py, unchanged)
  - Subscriptions         → Paystack  (bridge.py, unchanged)
"""
import logging
import uuid
import os
import hmac
import hashlib
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


def _active_provider() -> str:
    """Return the currently-configured payment provider name."""
    return os.environ.get("ACTIVE_ESCROW_PROVIDER", "paystack").lower()


def _credit_vendor_ledger(vendor_id: int, amount: float, reference_id: str, description: str):
    """Write a CREDIT entry to the vendor's ledger."""
    from sqlalchemy import func
    from app.models.finance import Ledger as L
    existing = L.query.filter_by(vendor_id=vendor_id, reference_id=reference_id, transaction_type='CREDIT').first()
    if existing:
        logging.info(f"[LEDGER] Already credited vendor {vendor_id} for ref {reference_id} — skipping duplicate")
        return
    # Calculate running balance
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


def _deliver_digital_products(order, escrow):
    """
    For orders containing digital products:
    - Immediately mark escrow as RELEASED (no delivery wait needed)
    - Credit vendor ledger
    - Email buyer the download link(s)
    - Mark order COMPLETED
    Returns True if any digital items were found and handled.
    """
    from app.models.product import Product as Prod
    from app.utils.email import send_siiqo_email
    from app.models.user import User

    digital_items = []
    for item in (order.items or []):
        p = db.session.get(Prod, item.product_id) if item.product_id else item.product
        if p and p.product_type == 'digital':
            digital_items.append((p, item))

    if not digital_items:
        return False

    # Build download list for email — HTML links so they are clickable in inbox
    download_links_html = "".join(
        f'<p style="margin:8px 0;"><strong>{p.name}</strong><br>'
        f'<a href="{p.file_url}" style="color:#E0921C;word-break:break-all;">{p.file_url}</a></p>'
        for p, _ in digital_items if p.file_url
    ) or "<p>The vendor will share your download link shortly via Siiqo chat.</p>"

    # Release escrow immediately — no physical delivery required
    net_amount = float(escrow.amount) - float(escrow.fee_amount or 0)
    escrow.status = EscrowStatus.RELEASED
    escrow.released_at = _utcnow()
    order.status = 'COMPLETED'

    # If paid via Paystack, handle vendor payout:
    # - If vendor has a subaccount_code → Paystack Split Payment already settled
    #   them directly at checkout. No manual transfer needed.
    # - Otherwise → fall back to manual Paystack Transfer (legacy flow).
    # NOTE: CRYPTO orders skip all Paystack payout — funds sit in Siiqo's Daya
    #       merchant balance and are paid out to the vendor separately via Daya.
    is_crypto = (order.payment_method or '').upper() == 'CRYPTO'
    is_flutterwave = (
        (order.payment_method or '').upper() == 'FLUTTERWAVE'
        or (escrow.transaction_number or '').startswith('FLW-')
    )
    is_paystack = not is_crypto and not is_flutterwave and (
        (order.payment_method or '').upper() == 'PAYSTACK'
        or (
            escrow.payscrow_transaction_id
            and not escrow.payscrow_transaction_id.startswith('ESC-')
            and not escrow.payscrow_transaction_id.startswith('DAYA-')
        )
    )
    if is_paystack:
        # Check if vendor was registered with a subaccount (split payment used)
        vendor_used_split = False
        try:
            bank_acc = VendorBankAccount.query.filter_by(
                vendor_id=order.vendor_id, is_default=True
            ).first() or VendorBankAccount.query.filter_by(
                vendor_id=order.vendor_id
            ).first()
            if bank_acc and bank_acc.paystack_subaccount_code:
                vendor_used_split = True
            else:
                from app.models.user import Storefront
                sf = Storefront.query.filter_by(vendor_id=order.vendor_id).first()
                if sf and sf.paystack_subaccount_code:
                    vendor_used_split = True
        except Exception:
            pass

        if vendor_used_split:
            logging.info(
                f"[DIGITAL] Order #{order.id} — vendor has subaccount, "
                "Paystack split already settled vendor. Skipping manual transfer."
            )
        else:
            # Legacy fallback: no subaccount, use manual transfer
            _paystack_payout_vendor(order, escrow)

    _credit_vendor_ledger(
        vendor_id=order.vendor_id,
        amount=net_amount,
        reference_id=escrow.transaction_number,
        description=f"Auto-released payout for digital Order #{order.id}",
    )

    from app.models.finance import Receipt
    if not Receipt.query.filter_by(order_id=order.id).first():
        db.session.add(Receipt(order_id=order.id))

    # Notify buyer with download link(s)
    buyer = db.session.get(User, order.buyer_id) if order.buyer_id else None
    download_msg = f"Your download link(s) for Order #{order.id} are ready."
    if order.buyer_id:
        db.session.add(Notification(
            user_id=order.buyer_id,
            title="Your Digital Download is Ready! 🎉",
            message=download_msg,
            type="ORDER",
            order_id=order.id,
        ))
    db.session.add(Notification(
        user_id=order.vendor_id,
        title="Digital Order Complete",
        message=f"Order #{order.id} completed automatically. ₦{net_amount:,.2f} credited.",
        type="ESCROW",
        order_id=order.id,
    ))

    buyer_email = (buyer.email if buyer else None) or getattr(order, 'buyer_email', None)
    if buyer_email:
        buyer_first_name = (buyer.first_name if buyer else None) or getattr(order, 'buyer_name', None) or "there"
        try:
            send_siiqo_email(
                to_email=buyer_email,
                subject=f"Your Digital Download – Order #{order.id} | Siiqo",
                template_name="system_notice",
                first_name=buyer_first_name,
                notice_text=(
                    f"Great news! Your payment for Order #{order.id} is confirmed.<br><br>"
                    f"Here are your download link(s):<br><br>{download_links_html}<br>"
                    "These links are yours to keep. If you have any issues accessing your files, "
                    "please contact the seller via the Siiqo chat."
                ),
            )
        except Exception as e:
            logging.warning(f"[EMAIL] digital download email failed Order #{order.id}: {e}")

    # Notify vendor
    vendor = db.session.get(User, order.vendor_id)
    if vendor and vendor.email:
        try:
            send_siiqo_email(
                to_email=vendor.email,
                subject=f"Digital Sale Complete – Order #{order.id} | Siiqo",
                template_name="system_notice",
                first_name=vendor.first_name or "Vendor",
                notice_text=(
                    f"Your digital product was purchased and delivered automatically.\n"
                    f"Order #{order.id} — ₦{net_amount:,.2f} credited to your ledger."
                ),
            )
        except Exception as e:
            logging.warning(f"[EMAIL] vendor digital sale email failed: {e}")

    logging.info(f"[DIGITAL] Auto-delivered and released Order #{order.id}")
    return True


def _deliver_service_products(order, escrow):
    """
    For orders containing service products:
    - Immediately mark escrow as RELEASED (no delivery wait needed)
    - Credit vendor ledger
    - Email buyer the booking link(s)
    - Mark order COMPLETED
    Returns True if any service items were found and handled.
    """
    from app.models.product import Product as Prod
    from app.utils.email import send_siiqo_email
    from app.models.user import User

    service_items = []
    for item in (order.items or []):
        p = db.session.get(Prod, item.product_id) if item.product_id else item.product
        if p and p.product_type == 'service':
            service_items.append((p, item))

    if not service_items:
        return False

    # Build booking list for email — HTML links so they are clickable in inbox
    booking_links_html = "".join(
        f'<p style="margin:8px 0;"><strong>{p.name}</strong><br>'
        f'<a href="{p.booking_link}" style="color:#E0921C;word-break:break-all;">{p.booking_link}</a></p>'
        for p, _ in service_items if p.booking_link
    ) or "<p>The vendor will reach out to you via Siiqo chat to schedule your service.</p>"

    # Release escrow immediately
    net_amount = float(escrow.amount) - float(escrow.fee_amount or 0)
    escrow.status = EscrowStatus.RELEASED
    escrow.released_at = _utcnow()
    order.status = 'COMPLETED'

    # If paid via Paystack, handle vendor payout:
    # - If vendor has a subaccount_code → Paystack Split Payment already settled
    #   them directly at checkout. No manual transfer needed.
    # - Otherwise → fall back to manual Paystack Transfer (legacy flow).
    # NOTE: CRYPTO orders skip all Paystack payout (funds held in Daya balance).
    is_crypto = (order.payment_method or '').upper() == 'CRYPTO'
    is_flutterwave = (
        (order.payment_method or '').upper() == 'FLUTTERWAVE'
        or (escrow.transaction_number or '').startswith('FLW-')
    )
    is_paystack = not is_crypto and not is_flutterwave and (
        (order.payment_method or '').upper() == 'PAYSTACK'
        or (
            escrow.payscrow_transaction_id
            and not escrow.payscrow_transaction_id.startswith('ESC-')
            and not escrow.payscrow_transaction_id.startswith('DAYA-')
        )
    )
    if is_paystack:
        vendor_used_split = False
        try:
            bank_acc = VendorBankAccount.query.filter_by(
                vendor_id=order.vendor_id, is_default=True
            ).first() or VendorBankAccount.query.filter_by(
                vendor_id=order.vendor_id
            ).first()
            if bank_acc and bank_acc.paystack_subaccount_code:
                vendor_used_split = True
            else:
                from app.models.user import Storefront
                sf = Storefront.query.filter_by(vendor_id=order.vendor_id).first()
                if sf and sf.paystack_subaccount_code:
                    vendor_used_split = True
        except Exception:
            pass

        if vendor_used_split:
            logging.info(
                f"[SERVICE] Order #{order.id} — vendor has subaccount, "
                "Paystack split already settled vendor. Skipping manual transfer."
            )
        else:
            _paystack_payout_vendor(order, escrow)

    _credit_vendor_ledger(
        vendor_id=order.vendor_id,
        amount=net_amount,
        reference_id=escrow.transaction_number,
        description=f"Auto-released payout for service Order #{order.id}",
    )

    from app.models.finance import Receipt
    if not Receipt.query.filter_by(order_id=order.id).first():
        db.session.add(Receipt(order_id=order.id))

    # Notify buyer with booking link(s)
    buyer = db.session.get(User, order.buyer_id) if order.buyer_id else None
    booking_msg = f"Your booking link(s) for Order #{order.id} are ready."
    if order.buyer_id:
        db.session.add(Notification(
            user_id=order.buyer_id,
            title="Your Service Booking Link is Ready! 📅",
            message=booking_msg,
            type="ORDER",
            order_id=order.id,
        ))
    db.session.add(Notification(
        user_id=order.vendor_id,
        title="Service Order Complete",
        message=f"Order #{order.id} completed automatically. ₦{net_amount:,.2f} credited.",
        type="ESCROW",
        order_id=order.id,
    ))

    buyer_email = (buyer.email if buyer else None) or getattr(order, 'buyer_email', None)
    if buyer_email:
        buyer_first_name = (buyer.first_name if buyer else None) or getattr(order, 'buyer_name', None) or "there"
        try:
            send_siiqo_email(
                to_email=buyer_email,
                subject=f"Book Your Service – Order #{order.id} | Siiqo",
                template_name="system_notice",
                first_name=buyer_first_name,
                notice_text=(
                    f"Great news! Your payment for Order #{order.id} is confirmed.<br><br>"
                    f"Please use the link(s) below to book your appointment:<br><br>{booking_links_html}<br>"
                    "If you have any issues booking the service, please message the vendor in Siiqo chat."
                ),
            )
        except Exception as e:
            logging.warning(f"[EMAIL] service booking email failed Order #{order.id}: {e}")

    # Notify vendor
    vendor = db.session.get(User, order.vendor_id)
    if vendor and vendor.email:
        try:
            send_siiqo_email(
                to_email=vendor.email,
                subject=f"Service Booking Sale – Order #{order.id} | Siiqo",
                template_name="system_notice",
                first_name=vendor.first_name or "Vendor",
                notice_text=(
                    f"Your service product was purchased and booking links delivered automatically.\n"
                    f"Order #{order.id} — ₦{net_amount:,.2f} credited to your ledger."
                ),
            )
        except Exception as e:
            logging.warning(f"[EMAIL] vendor service sale email failed: {e}")

    logging.info(f"[SERVICE] Auto-delivered and released Order #{order.id}")
    return True


def _deliver_event_tickets(order, escrow):
    """
    For orders containing event tickets:
    - Activate pending tickets via activate_tickets_for_order(order.id)
    - Immediately mark escrow as RELEASED (no physical delivery wait needed)
    - Credit vendor ledger
    - Email buyer and vendor
    - Mark order COMPLETED
    Returns True if any event tickets were found and handled.
    """
    from app.models.event import TicketPurchase
    from app.routes.events import activate_tickets_for_order
    from app.models.user import User
    from app.models.finance import Receipt

    tickets = TicketPurchase.query.filter_by(order_id=order.id).all()
    if not tickets:
        return False

    # Activate tickets (switches status PENDING -> ACTIVE, updates sold counts, emails buyer QR code)
    activate_tickets_for_order(order.id)

    # Release escrow immediately — events are instant
    net_amount = float(escrow.amount) - float(escrow.fee_amount or 0)
    escrow.status = EscrowStatus.RELEASED
    escrow.released_at = _utcnow()
    order.status = 'COMPLETED'

    is_crypto = (order.payment_method or '').upper() == 'CRYPTO'
    is_flutterwave = (
        (order.payment_method or '').upper() == 'FLUTTERWAVE'
        or (escrow.transaction_number or '').startswith('FLW-')
    )
    is_paystack = not is_crypto and not is_flutterwave and (
        (order.payment_method or '').upper() == 'PAYSTACK'
        or (
            escrow.payscrow_transaction_id
            and not escrow.payscrow_transaction_id.startswith('ESC-')
            and not escrow.payscrow_transaction_id.startswith('DAYA-')
        )
    )
    if is_paystack:
        vendor_used_split = False
        try:
            bank_acc = VendorBankAccount.query.filter_by(
                vendor_id=order.vendor_id, is_default=True
            ).first() or VendorBankAccount.query.filter_by(
                vendor_id=order.vendor_id
            ).first()
            if bank_acc and bank_acc.paystack_subaccount_code:
                vendor_used_split = True
            else:
                from app.models.user import Storefront
                sf = Storefront.query.filter_by(vendor_id=order.vendor_id).first()
                if sf and sf.paystack_subaccount_code:
                    vendor_used_split = True
        except Exception:
            pass

        if vendor_used_split:
            logging.info(f"[EVENT] Order #{order.id} — vendor has subaccount, Paystack split already settled vendor.")
        else:
            _paystack_payout_vendor(order, escrow)

    _credit_vendor_ledger(
        vendor_id=order.vendor_id,
        amount=net_amount,
        reference_id=escrow.transaction_number,
        description=f"Auto-released payout for Event Ticket Order #{order.id}",
    )

    if not Receipt.query.filter_by(order_id=order.id).first():
        db.session.add(Receipt(order_id=order.id))

    if order.buyer_id:
        db.session.add(Notification(
            user_id=order.buyer_id,
            title="Event Tickets Ready! 🎟️",
            message=f"Your tickets for Order #{order.id} are active. Access them in My Tickets.",
            type="ORDER",
            order_id=order.id,
        ))
    db.session.add(Notification(
        user_id=order.vendor_id,
        title="Event Ticket Sale Complete 🎟️",
        message=f"Order #{order.id} ticket sale completed. ₦{net_amount:,.2f} credited.",
        type="ESCROW",
        order_id=order.id,
    ))

    logging.info(f"[EVENT] Auto-delivered and released Order #{order.id}")
    return True

# ---------------------------------------------------------------------------
# POST /escrow/initiate
# ---------------------------------------------------------------------------

@escrow_bp.route('/initiate', methods=['POST'])
@jwt_required(optional=True)
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
        if order.buyer_id:
            if not user_id or str(order.buyer_id) != str(user_id):
                return jsonify({"message": "Unauthorized"}), 403

    # ── PHYSICAL PRODUCT GUARD ────────────────────────────────────────────────
    # Paystack cannot hold escrow for physical products — company does not hold
    # an escrow license for physical goods on Paystack.
    # Physical products MUST use Bank Transfer & Crypto (Daya) or Pay on Delivery (POD).
    from app.models.order import OrderItem
    from app.models.product import Product as _Prod
    for order in orders:
        for item in order.items:
            p = db.session.get(_Prod, item.product_id) if item.product_id else item.product
            p_type = (p.product_type if p else 'physical') or 'physical'
            if p_type == 'physical':
                return jsonify({
                    "message": (
                        "Paystack cannot be used for physical products. "
                        "Please use Bank Transfer & Crypto (Daya) or Pay on Delivery instead."
                    ),
                    "code": "PAYSTACK_PHYSICAL_BLOCKED",
                }), 400
    # ─────────────────────────────────────────────────────────────────────────

    # Validate bank accounts ONLY for Payscrow (physical orders)
    # Paystack handles digital/service orders and uses its own recipient API
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
    provider = get_escrow_provider(orders)
    result = provider.initiate_transaction(orders, existing_txn_number)
    
    if not result.get("success"):
        return jsonify({"message": result.get("error_message") or "Escrow init failed"}), 400

    txn_map = {e.order_id: e for e in escrow_txns}
    
    for order in orders:
        escrow_txn = txn_map.get(order.id)
        if not escrow_txn:
            # fee_amount for an individual order in a master transaction
            # the result['fee_amount'] is total, so we calculate proportional
            fee_rate = 0.03  # Flat 3% Safe Pay fee for all vendors
            individual_fee = round(float(order.total_amount) * fee_rate, 2)
            escrow_txn = EscrowTransaction(
                order_id=order.id,
                transaction_number=result['transaction_number'],
                status=EscrowStatus.PENDING_PAYMENT,
                amount=float(order.total_amount),
                fee_percent=fee_rate * 100,
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
@jwt_required(optional=True)
def escrow_status():
    txn_param = request.args.get('txn') or request.args.get('reference') or request.args.get('trxref') or request.args.get('txnref')
    order_id_param = request.args.get('order_id') or request.args.get('ref')

    escrow = None

    if txn_param:
        escrow = EscrowTransaction.query.filter_by(transaction_number=txn_param).first()

    if not escrow and order_id_param:
        if str(order_id_param).isdigit():
            escrow = EscrowTransaction.query.filter_by(order_id=int(order_id_param)).first()
        else:
            escrow = EscrowTransaction.query.filter_by(transaction_number=str(order_id_param)).first()

    if not escrow and txn_param and str(txn_param).isdigit():
        escrow = EscrowTransaction.query.filter_by(order_id=int(txn_param)).first()

    if not escrow:
        return jsonify({"message": "Transaction not found"}), 404

    # If escrow is still PENDING_PAYMENT, proactively verify with Flutterwave or Paystack
    if escrow.status == EscrowStatus.PENDING_PAYMENT and escrow.transaction_number:
        if escrow.transaction_number.startswith("FLW-"):
            try:
                from app.routes.payments import _handle_flutterwave_payment_confirmed
                from app.services import flutterwave_service as _flw
                tx_id = request.args.get("transaction_id") or request.args.get("tx_id")
                if tx_id:
                    verif = _flw.verify_transaction(tx_id)
                    if verif.get("success") and verif.get("status") == "successful":
                        _handle_flutterwave_payment_confirmed(escrow.transaction_number, str(tx_id), verif)
                        db.session.refresh(escrow)
            except Exception as _flw_err:
                logging.warning(f"[ESCROW STATUS] Flutterwave proactive verification failed: {_flw_err}")
        else:
            try:
                from app.services.escrow.paystack_provider import PaystackProvider
                verification = PaystackProvider().verify_transaction(escrow.transaction_number)
                if verification.get("success"):
                    escrow.status = EscrowStatus.IN_ESCROW
                    escrow.paid_at = _utcnow()
                    escrow.payscrow_transaction_id = escrow.transaction_number
                    if escrow.order:
                        escrow.order.status = 'PAID'
                        db.session.flush()

                        is_event = _deliver_event_tickets(escrow.order, escrow)
                        is_digital = False
                        if not is_event:
                            is_digital = _deliver_digital_products(escrow.order, escrow)
                        if not is_event and not is_digital:
                            _deliver_service_products(escrow.order, escrow)

                    db.session.commit()
                    logging.info(f"[ESCROW STATUS] Proactively verified & activated transaction {escrow.transaction_number}")
            except Exception as _sync_err:
                logging.warning(f"[ESCROW STATUS] Paystack verification check failed: {_sync_err}")

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

                    # ── Digital/Service product: auto-deliver and release immediately ──
                    # Commit what we have so far before calling delivery helpers
                    db.session.flush()
                    is_digital_order = _deliver_digital_products(order, escrow)
                    is_service_order = False
                    is_event_order = False
                    if not is_digital_order:
                        is_service_order = _deliver_service_products(order, escrow)
                    if not is_digital_order and not is_service_order:
                        is_event_order = _deliver_event_tickets(order, escrow)

                    if not is_digital_order and not is_service_order and not is_event_order:
                        # Physical: normal logistics flow
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

                        if order.buyer_id:
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

                    processed_orders.append((escrow, order, is_digital_order or is_service_order))

        db.session.commit()

        from app.utils.email import send_siiqo_email
        from app.models.user import User

        for escrow, order, is_digital_order in processed_orders:
            is_digital_or_service = all(
                (item.product.product_type if item.product else 'physical') in ('digital', 'service')
                for item in order.items
            )
            buyer = db.session.get(User, order.buyer_id) if order.buyer_id else None
            buyer_email = (buyer.email if buyer else None) or getattr(order, 'buyer_email', None)
            if buyer_email:
                buyer_name = (buyer.first_name if buyer else None) or getattr(order, 'buyer_name', None) or "there"
                try:
                    send_siiqo_email(
                        to_email=buyer_email,
                        subject=f"Order Confirmation #{order.id} - Siiqo",
                        template_name="order_confirmation",
                        first_name=buyer_name,
                        order_id=order.id,
                        payment_method="ESCROW",
                        is_digital_or_service=is_digital_or_service,
                    )
                except Exception as e:
                    logging.warning(f"[EMAIL] buyer confirm email failed Order #{order.id}: {e}")

            vendor = db.session.get(User, order.vendor_id)
            if vendor and vendor.email:
                b_name = (buyer.full_name if buyer else None) or getattr(order, 'buyer_name', None) or "Customer"
                b_phone = (buyer.phone if buyer else None) or getattr(order, 'buyer_phone', None) or getattr(order, 'delivery_phone', None) or ""
                try:
                    send_siiqo_email(
                        to_email=vendor.email,
                        subject="New Order - Siiqo",
                        template_name="order_received_vendor",
                        first_name=vendor.first_name or "Vendor",
                        order_id=order.id,
                        total_amount=f"₦{float(order.total_amount):,.2f}",
                        payment_method="ESCROW",
                        is_digital_or_service=is_digital_or_service,
                        buyer_name=b_name,
                        buyer_email=buyer_email or "",
                        buyer_phone=b_phone,
                    )
                except Exception as e:
                    logging.warning(f"[EMAIL] vendor email failed Order #{order.id}: {e}")

    return jsonify({"received": True}), 200


def _paystack_payout_vendor(order, escrow):
    """
    Push vendor's net share via Paystack Transfers API.
    Used for instant payouts of digital/service orders or upon buyer delivery confirmation.
    """
    net_amount = float(escrow.amount) - float(escrow.fee_amount or 0)
    
    bank_acc = VendorBankAccount.query.filter_by(
        vendor_id=order.vendor_id, is_default=True
    ).first()
    if not bank_acc:
        bank_acc = VendorBankAccount.query.filter_by(
            vendor_id=order.vendor_id
        ).first()

    if bank_acc:
        if not bank_acc.recipient_code and bank_acc.account_number and bank_acc.bank_code:
            # Self-healing payout: create Paystack transfer recipient on-the-fly
            try:
                from app.services.escrow.paystack_provider import ensure_paystack_transfer_recipient
                rec_res = ensure_paystack_transfer_recipient(
                    account_number=bank_acc.account_number,
                    bank_code=bank_acc.bank_code,
                    account_name=bank_acc.account_name or "Vendor",
                )
                if rec_res.get("success"):
                    bank_acc.recipient_code = rec_res["recipient_code"]
                    if not bank_acc.account_name or bank_acc.account_name == "Vendor":
                        bank_acc.account_name = rec_res.get("account_name") or bank_acc.account_name
                    db.session.commit()
                    logging.info(
                        f"[PAYSTACK SELF-HEALING] Auto-created recipient {bank_acc.recipient_code} "
                        f"for vendor {order.vendor_id} on Order #{order.id}"
                    )
            except Exception as _heal_exc:
                logging.warning(f"[PAYSTACK SELF-HEALING] Failed to auto-create recipient: {_heal_exc}")

        if bank_acc.recipient_code:
            from app.services.escrow.paystack_provider import paystack_transfer_to_vendor
            import uuid
            transfer_result = paystack_transfer_to_vendor(
                recipient_code=bank_acc.recipient_code,
                amount_ngn=net_amount,
                reference=f"PAYOUT-{order.id}-{uuid.uuid4().hex[:6].upper()}",
                reason=f"Siiqo payout for Order #{order.id}",
            )
            if not transfer_result.get("success"):
                logging.error(
                    f"[PAYSTACK TRANSFER] Failed for Order #{order.id}: "
                    f"{transfer_result.get('error_message')}"
                )
                return False
            return True

    logging.warning(
        f"[RELEASE] Vendor {order.vendor_id} has no valid bank account or recipient_code. "
        "Crediting ledger only — no Paystack transfer."
    )
    return False


def execute_order_escrow_release(order, escrow, source="admin"):
    """
    Unified, idempotent order completion & payout pipeline.
    1. Sets escrow.status = RELEASED
    2. Credits vendor ledger balance
    3. Fires transfer payout (Paystack or Daya based on order gateway)
    4. Sets order.status = 'COMPLETED'
    5. Sends notifications to buyer & vendor
    """
    if not order or not escrow:
        return False

    if escrow.status == EscrowStatus.RELEASED:
        order.status = 'COMPLETED'
        db.session.commit()
        return True

    net_amount = float(escrow.amount) - float(escrow.fee_amount or 0)
    escrow.status = EscrowStatus.RELEASED
    escrow.released_at = _utcnow()
    order.status = 'COMPLETED'

    _credit_vendor_ledger(
        vendor_id=order.vendor_id,
        amount=net_amount,
        reference_id=escrow.transaction_number or f"ORD-{order.id}",
        description=f"Payout for Order #{order.id} ({source})",
    )

    # Determine payout channel: Daya (crypto/NGN bank-transfer onramp) vs Paystack
    # IMPORTANT: 'DAYA_BANK_TRANSFER' is set by payments.py when buyer pays via Daya
    # NGN virtual account. It MUST be included here or payout is silently skipped.
    DAYA_PAYMENT_METHODS = ('CRYPTO', 'DAYA', 'USDT', 'USDC', 'DAYA_BANK_TRANSFER')
    is_daya_order = (
        (order.payment_method or '') in DAYA_PAYMENT_METHODS
        or getattr(escrow, 'gateway', None) == 'DAYA'
        or getattr(order, 'crypto_hash', None)
        or (escrow.transaction_number or '').startswith('DYA-')
        or (escrow.payscrow_transaction_id or '').startswith('DAYA-')
    )

    is_flutterwave_order = (
        (order.payment_method or '').upper() == 'FLUTTERWAVE'
        or (escrow.transaction_number or '').startswith('FLW-')
    )

    if is_flutterwave_order:
        # Flutterwave channel
        vendor_already_paid_via_split = False
        try:
            bank_acc = VendorBankAccount.query.filter_by(vendor_id=order.vendor_id, is_default=True).first() or \
                       VendorBankAccount.query.filter_by(vendor_id=order.vendor_id).first()
            if bank_acc and bank_acc.flw_subaccount_id:
                vendor_already_paid_via_split = True
        except Exception as _sub_err:
            logging.warning(f"[ESCROW RELEASE] FLW subaccount check warning: {_sub_err}")

        if not vendor_already_paid_via_split and bank_acc and bank_acc.bank_code and bank_acc.account_number:
            try:
                from app.services import flutterwave_service as _flw
                import uuid as _uuid
                payout_ref = f"WD-FLW-{order.id}-{_uuid.uuid4().hex[:6].upper()}"
                flw_tx = _flw.transfer_to_vendor(
                    account_bank=bank_acc.bank_code,
                    account_number=bank_acc.account_number,
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
                    logging.info(f"[ESCROW RELEASE] Flutterwave transfer queued for Order #{order.id}")
                else:
                    logging.warning(f"[ESCROW RELEASE] Flutterwave transfer failed for Order #{order.id}: {flw_tx.get('error_message')}")
            except Exception as _flw_tr_err:
                logging.warning(f"[ESCROW RELEASE] Flutterwave transfer error for Order #{order.id}: {_flw_tr_err}")
    elif is_daya_order:
        try:
            from app.routes.payments import _payout_vendor_via_daya
            _payout_vendor_via_daya(order, escrow)
        except Exception as _d_err:
            logging.warning(f"[ESCROW RELEASE] Daya payout error for Order #{order.id}: {_d_err}")
    else:
        # Paystack channel
        vendor_already_paid_via_split = False
        try:
            bank_acc = VendorBankAccount.query.filter_by(vendor_id=order.vendor_id, is_default=True).first() or \
                       VendorBankAccount.query.filter_by(vendor_id=order.vendor_id).first()
            if bank_acc and bank_acc.paystack_subaccount_code:
                vendor_already_paid_via_split = True
            else:
                from app.models.user import Storefront
                sf = Storefront.query.filter_by(vendor_id=order.vendor_id).first()
                if sf and sf.paystack_subaccount_code:
                    vendor_already_paid_via_split = True
        except Exception as _sub_err:
            logging.warning(f"[ESCROW RELEASE] Subaccount check warning: {_sub_err}")

        if not vendor_already_paid_via_split:
            _paystack_payout_vendor(order, escrow)

    # In-app notifications
    try:
        from app.models.communication import Notification
        if order.buyer_id:
            db.session.add(Notification(
                user_id=order.buyer_id,
                title="Order Completed",
                message=f"Order #{order.id} is marked complete.",
                type="ORDER",
                order_id=order.id,
            ))
        db.session.add(Notification(
            user_id=order.vendor_id,
            title="Payment Released",
            message=f"Payment of ₦{net_amount:,.2f} for Order #{order.id} has been released to your account.",
            type="ESCROW",
            order_id=order.id,
        ))
    except Exception as _notif_err:
        logging.warning(f"[ESCROW RELEASE] Notification warning: {_notif_err}")

    db.session.commit()
    logging.info(f"[ESCROW RELEASE] Released Order #{order.id} and paid vendor {order.vendor_id} via {source}")
    return True


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
    # All Daya-originated payments: crypto direct, USDT/USDC, and NGN bank
    # transfer via Daya virtual account (payment_method='DAYA_BANK_TRANSFER').
    DAYA_PAYMENT_METHODS = ('CRYPTO', 'DAYA', 'USDT', 'USDC', 'DAYA_BANK_TRANSFER')
    is_daya_order = (
        (order.payment_method or '') in DAYA_PAYMENT_METHODS
        or (escrow.transaction_number or '').startswith('DYA-')
        or (escrow.payscrow_transaction_id or '').startswith('DAYA-')
    )

    is_paystack_order = (
        not is_daya_order and (
            (order.payment_method or '').upper() == 'PAYSTACK'
            or (escrow.transaction_number and escrow.transaction_number.startswith('ORD-'))
        )
    )

    if is_daya_order:
        # ── Daya payout — funds are in Siiqo's Daya collection/withdrawal balance ──
        from app.routes.payments import _payout_vendor_via_daya
        _payout_vendor_via_daya(order, escrow)
    elif is_paystack_order:
        # ── Paystack split payment ────────────────────────────────────────────
        # Check if vendor was paid via Paystack split at checkout time.
        # If paystack_subaccount_code exists, Paystack already settled the
        # vendor's 94% directly to their bank when the buyer paid.
        # Calling _paystack_payout_vendor() again would be a double payment.
        vendor_already_paid_via_split = False
        try:
            bank_acc = VendorBankAccount.query.filter_by(
                vendor_id=order.vendor_id, is_default=True
            ).first() or VendorBankAccount.query.filter_by(
                vendor_id=order.vendor_id
            ).first()
            if bank_acc and bank_acc.paystack_subaccount_code:
                vendor_already_paid_via_split = True
            else:
                from app.models.user import Storefront
                sf = Storefront.query.filter_by(vendor_id=order.vendor_id).first()
                if sf and sf.paystack_subaccount_code:
                    vendor_already_paid_via_split = True
        except Exception as _exc:
            logging.warning("[RELEASE] Could not check subaccount for vendor %s: %s",
                            order.vendor_id, _exc)

        if vendor_already_paid_via_split:
            logging.info(
                "[RELEASE] Order #%s — vendor already paid via Paystack split at checkout. "
                "Skipping manual transfer.", order.id
            )
        else:
            # Legacy non-split flow — manual Paystack transfer
            _paystack_payout_vendor(order, escrow)
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

    try:
        from app.services.event_logger import log_platform_event, log_trust_evidence
        log_platform_event(
            event_name="order_completed",
            order_id=order.id,
            business_id=order.vendor_id,
            user_id=order.buyer_id,
            source="escrow",
            properties={"amount": str(escrow.amount), "transaction_number": escrow.transaction_number}
        )
        log_trust_evidence(
            business_id=order.vendor_id,
            evidence_type="order_fulfilled",
            provenance="siiqo_transaction_verified",
            source="escrow_transactions",
            source_id=str(escrow.id),
            properties={"order_id": order.id, "amount": str(escrow.amount)}
        )
    except Exception as ev_err:
        logging.error(f"[TELEMETRY ERR] Failed to log order completion evidence: {ev_err}")

    # ── First-sale celebration ─────────────────────────────────────────────
    try:
        from app.models.order import Order as _Order
        vendor_order_count = _Order.query.filter_by(vendor_id=order.vendor_id).count()
        if vendor_order_count == 1:
            vendor_user = db.session.get(User, order.vendor_id)
            db.session.add(Notification(
                user_id=order.vendor_id,
                title="🎉 Your First Sale!",
                message=(
                    f"Congratulations! You just made your first sale on Siiqo — Order #{order.id}. "
                    "Check your orders page to confirm delivery and get paid."
                ),
                type="ORDER",
                order_id=order.id,
            ))
            if vendor_user and vendor_user.email:
                from app.utils.email import send_siiqo_email as _send_email
                try:
                    _send_email(
                        to_email=vendor_user.email,
                        subject="Your First Sale on Siiqo! 🎉",
                        template_name="first_sale",
                        first_name=vendor_user.first_name or "Vendor",
                        order_id=order.id,
                        total_amount=f"₦{float(order.total_amount):,.2f}",
                    )
                except Exception as mail_err:
                    logging.warning(f"[FIRST SALE EMAIL ERR] {mail_err}")
    except Exception as ex:
        logging.error(f"[FIRST SALE ERR] {ex}")

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
    if order.buyer_id:
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

    buyer = db.session.get(User, order.buyer_id) if order.buyer_id else None
    buyer_email = (buyer.email if buyer else None) or getattr(order, 'buyer_email', None)
    if buyer_email:
        buyer_name = (buyer.first_name if buyer else None) or getattr(order, 'buyer_name', None) or "Buyer"
        try:
            send_siiqo_email(
                to_email=buyer_email,
                subject="Siiqo - Order Completed",
                template_name="system_notice",
                first_name=buyer_name,
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


# ---------------------------------------------------------------------------
# TOKEN-BASED GUEST DELIVERY CONFIRMATION
# Allows guest buyers to view order summary and confirm receipt in 1 click
# without requiring an account or login.
# ---------------------------------------------------------------------------

def generate_order_token(order_id: int) -> str:
    secret = (os.environ.get("SECRET_KEY") or "siiqo-secret-key-salt-2026").encode()
    return hmac.new(secret, f"order-confirm-{order_id}".encode(), hashlib.sha256).hexdigest()[:32]


@escrow_bp.route('/order-details-by-token', methods=['GET'])
@jwt_required(optional=True)
def get_order_details_by_token():
    """
    Public token-authenticated or user-authenticated endpoint for delivery confirmation page.
    Query params: order_id, token (optional if authenticated as buyer/vendor/admin)
    """
    order_id = request.args.get('order_id', type=int)
    token = (request.args.get('token') or '').strip()
    user_id = get_jwt_identity()

    if not order_id:
        return jsonify({"message": "order_id is required"}), 400

    try:
        from app.models.order import Order
        order = db.session.get(Order, order_id)
        if not order:
            return jsonify({"message": "Order not found"}), 404

        token_valid = False
        if token:
            expected_token = generate_order_token(order_id)
            token_valid = hmac.compare_digest(expected_token, token)

        is_authorized_user = False
        if user_id:
            try:
                u_id = int(user_id)
                if u_id == order.buyer_id or u_id == order.vendor_id:
                    is_authorized_user = True
                else:
                    from app.models.user import User, UserRole
                    u = db.session.get(User, u_id)
                    if u and u.role == UserRole.ADMIN:
                        is_authorized_user = True
            except Exception:
                pass

        if not token_valid and not is_authorized_user:
            return jsonify({"message": "Invalid or missing confirmation security token. Please check your link or log in."}), 403

        vendor_name = "Vendor"
        try:
            if order.vendor and hasattr(order.vendor, 'storefront') and order.vendor.storefront:
                vendor_name = order.vendor.storefront.store_name
            elif order.vendor:
                vendor_name = order.vendor.first_name or "Vendor"
        except Exception:
            vendor_name = "Vendor"

        items_list = []
        for it in order.items:
            unit_price = getattr(it, 'price_at_purchase', getattr(it, 'price', 0))
            items_list.append({
                "name": it.product.name if it.product else "Item",
                "quantity": it.quantity,
                "price": float(unit_price or 0),
            })

        return jsonify({
            "status": "success",
            "order": {
                "id": order.id,
                "status": order.status,
                "total_amount": float(order.total_amount),
                "buyer_name": order.buyer_name,
                "buyer_email": order.buyer_email,
                "buyer_phone": order.buyer_phone,
                "vendor_name": vendor_name,
                "created_at": order.created_at.isoformat() if order.created_at else None,
                "items": items_list,
            }
        }), 200
    except Exception as e:
        import traceback
        logging.error(f"[ESCROW] Error loading order details for order #{order_id}: {e}\n{traceback.format_exc()}")
        return jsonify({"message": f"Could not load order #{order_id}: {str(e)}"}), 500


@escrow_bp.route('/confirm-delivery-by-token', methods=['POST'])
@jwt_required(optional=True)
def confirm_delivery_by_token():
    """
    Delivery confirmation endpoint.
    Authenticated via token OR buyer JWT.
    Releases escrow and triggers vendor payout.
    """
    data = request.get_json() or {}
    order_id = data.get('order_id')
    token = (data.get('token') or '').strip()
    user_id = get_jwt_identity()

    if not order_id:
        return jsonify({"message": "order_id is required"}), 400

    try:
        order_id = int(order_id)
    except (ValueError, TypeError):
        return jsonify({"message": "Invalid order_id"}), 400

    from app.models.order import Order
    from app.models.escrow import EscrowTransaction

    order = db.session.get(Order, order_id)
    if not order:
        return jsonify({"message": "Order not found"}), 404

    token_valid = False
    if token:
        expected_token = generate_order_token(order_id)
        token_valid = hmac.compare_digest(expected_token, token)

    is_buyer = False
    if user_id:
        try:
            u_id = int(user_id)
            if u_id == order.buyer_id:
                is_buyer = True
            else:
                from app.models.user import User, UserRole
                u = db.session.get(User, u_id)
                if u and u.role == UserRole.ADMIN:
                    is_buyer = True
        except Exception:
            pass

    if not token_valid and not is_buyer:
        return jsonify({"message": "Invalid or expired confirmation token. Only the buyer can confirm package receipt."}), 403

    escrow = EscrowTransaction.query.filter_by(order_id=order.id).first()
    if not escrow:
        return jsonify({"message": "No escrow record found for this order"}), 404

    if escrow.status == EscrowStatus.RELEASED or order.status == 'COMPLETED':
        return jsonify({
            "status": "success",
            "message": "Order is already completed and received. Thank you!",
            "already_completed": True,
        }), 200

    success = execute_order_escrow_release(order, escrow, source="guest_buyer_token")
    if not success:
        return jsonify({"message": "Could not complete release. Please contact support."}), 500

    return jsonify({
        "status": "success",
        "message": "Delivery confirmed! Payment has been released to the vendor. Thank you for shopping on Siiqo.",
    }), 200

