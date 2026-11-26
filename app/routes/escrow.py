"""
escrow.py — Escrow lifecycle routes
Handles: initiate, status, Paystack webhook, Flutterwave webhook, release, dispute, admin actions

Payment provider split:
  - Marketplace cart checkout (digital, service, event) → Flutterwave or Paystack (card)
  - Marketplace cart checkout (physical)               → Daya (bank transfer / crypto) or POD
  - Payment Links (/pay/[slug])                        → Daya, Flutterwave, or Paystack
  - Subscriptions                                      → Paystack (bridge.py)

NOTE: Payscrow has been fully removed. payscrow_ref and payscrow_transaction_id DB columns
      are kept as-is (renamed would require a migration) but now store generic provider refs.
"""
import logging
import uuid
import os
import hmac
import hashlib
import html as _html
from datetime import datetime, timedelta, timezone

from flask import Blueprint, request, jsonify
from flask_jwt_extended import jwt_required, get_jwt_identity

from app.extensions import db, limiter
from app.models.order import Order
from app.models.escrow import EscrowTransaction, EscrowStatus
from app.models.finance import Ledger, Receipt
from app.models.communication import Notification
from app.models.withdrawal import PODPayment, VendorBankAccount
import requests

escrow_bp = Blueprint('escrow', __name__)


def _utcnow():
    return datetime.now(timezone.utc)


def _dispute_blocks_payout(order, escrow) -> bool:
    """True while the order's money is frozen by a dispute (app/services/disputes.py)."""
    from app.services.disputes import payout_blocked
    return payout_blocked(order, escrow)


def _verification_holds_payout(order, escrow, net_amount) -> bool:
    """Mandatory verification (D11, VERIFICATION_REQUIRED_FOR): True when this
    payout must wait until the seller verifies. It is recorded as held and kept
    out of the withdrawable balance, then sent once they verify
    (app/services/verification_requirement.py). Always False with the flag at
    its default, none."""
    from app.services.verification_requirement import hold_payout
    return hold_payout(order, escrow, net_amount)


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


def _debit_vendor_ledger(vendor_id: int, amount: float, reference_id: str, description: str):
    """Write a DEBIT entry to the vendor's ledger when funds are paid out via external gateway/transfer."""
    from sqlalchemy import func
    from app.models.finance import Ledger as L
    existing = L.query.filter_by(vendor_id=vendor_id, reference_id=reference_id, transaction_type='DEBIT').first()
    if existing:
        logging.info(f"[LEDGER] Already debited vendor {vendor_id} for ref {reference_id} — skipping duplicate")
        return
    credits = db.session.query(func.sum(L.amount)).filter_by(
        vendor_id=vendor_id, transaction_type='CREDIT'
    ).scalar() or 0
    debits = db.session.query(func.sum(L.amount)).filter_by(
        vendor_id=vendor_id, transaction_type='DEBIT'
    ).scalar() or 0
    balance_after = float(credits) - float(debits) - amount

    db.session.add(Ledger(
        vendor_id=vendor_id,
        transaction_type='DEBIT',
        amount=amount,
        description=description,
        reference_id=reference_id,
        balance_after=balance_after,
    ))


def _vendor_item_net_amount(order, escrow) -> float:
    """Item part of the seller's share: escrow amount - Siiqo's 3%. In naira
    for a foreign-currency order whose payment-time rate is known
    (app/services/fx.py); otherwise the escrow's face value, as before."""
    from app.services import fx
    converted = fx.naira_net(order, escrow)
    if converted is None and fx.order_currency(order, escrow) != 'NGN':
        # Paths that confirmed payment without capturing a rate get one now.
        fx.capture_for_order(order, escrow)
        converted = fx.naira_net(order, escrow)
    if converted is not None:
        return float(converted)
    return float(escrow.amount) - float(escrow.fee_amount or 0)


def _vendor_net_amount(order, escrow) -> float:
    """Seller's payout for this order, as used for the ledger and every payout
    rail: item amount - Siiqo's 3% + the seller's own delivery fee (whole, no
    3%; app/services/delivery_money.py). The delivery fee is already naira.
    After release it counts only when credited (``delivery_fee:<order_id>``),
    so orders released before this rule are never paid it retroactively."""
    from app.services.delivery_money import seller_delivery_share
    return round(_vendor_item_net_amount(order, escrow) + float(seller_delivery_share(order, escrow)), 2)


def _payout_amount_or_hold(order, escrow):
    """Convert or hold. Payout transfers send naira:
    - NGN order: returns the net amount.
    - Foreign-currency order with a payment-time rate on the order: returns
      the net amount converted to naira (half-up, 2 dp).
    - Foreign-currency order without a usable rate: held for Siiqo support
      instead of sending its foreign face value as naira. A matching DEBIT
      keeps the held amount out of the withdrawable balance. Returns None."""
    from app.services import fx
    currency = (escrow.currency or 'NGN').upper()
    net_amount = _vendor_net_amount(order, escrow)
    if currency == 'NGN':
        return net_amount
    if fx.naira_net(order, escrow) is not None:
        logging.info(
            f"[FX] Order #{order.id} paid in {currency}: paying out NGN {net_amount:,.2f} "
            f"at {order.fx_rate_to_ngn} NGN/{currency}"
        )
        return net_amount
    logging.warning(
        f"[FX HOLD] Order #{order.id} paid in {currency} {net_amount:,.2f} — payout held for support"
    )
    _debit_vendor_ledger(
        vendor_id=order.vendor_id,
        amount=net_amount,
        reference_id=f"FX-HOLD-{escrow.transaction_number or order.id}",
        description=f"Held for conversion: Order #{order.id} was paid in {currency}",
    )
    db.session.add(Notification(
        user_id=order.vendor_id,
        title="Payout Being Processed",
        message=(
            f"Order #{order.id} was paid in {currency}. Our team will convert it and send your payout. "
            "Contact support@siiqo.com with your order number if you have questions."
        ),
        type="ESCROW",
        order_id=order.id,
    ))
    return None


def _hold_if_foreign_currency(order, escrow) -> bool:
    """True when the payout was held (see _payout_amount_or_hold)."""
    return _payout_amount_or_hold(order, escrow) is None


def lock_escrow(escrow):
    """Lock this escrow's row until the transaction ends (SELECT ... FOR UPDATE
    on Postgres) and reload its current state. Every path that moves money for
    an escrow (payment confirmation that releases, buyer/token/admin/auto
    release) calls this before checking the status, so two concurrent calls
    run one after the other and the second sees the first one's result."""
    if escrow is None or getattr(escrow, "id", None) is None:
        return escrow
    return (db.session.query(EscrowTransaction).filter_by(id=escrow.id)
            .with_for_update().populate_existing().first())


def payout_transfer_reference(prefix: str, order, recipient_key: str) -> str:
    """Deterministic transfer reference for one order and one recipient, so a
    repeated payout call reuses the same reference and the gateway refuses a
    second transfer instead of sending the money twice."""
    digest = hashlib.sha256(f"{order.id}:{recipient_key or ''}".encode()).hexdigest()[:10].upper()
    return f"{prefix}-{order.id}-{digest}"


def payout_debit_reference(order, escrow) -> str:
    return f"PAYOUT-{escrow.transaction_number or order.id}"


def _track_order_transfer(order, escrow, gateway, reference, amount, transfer_id=None, status=None):
    """Store an accepted order payout transfer so its final status is followed
    (app/services/payout_tracking.py). Never raises: the money already left."""
    try:
        from app.services.payout_tracking import record_transfer
        record_transfer(kind="order", user_id=order.vendor_id, gateway=gateway, reference=reference,
                        amount=amount, gateway_transfer_id=transfer_id, order_id=order.id,
                        debit_reference=payout_debit_reference(order, escrow), gateway_status=status)
    except Exception as exc:  # noqa: BLE001
        logging.error(f"[PAYOUT TRACK] Could not record {gateway} transfer {reference} for Order #{order.id}: {exc}")


def _record_payout_debit(order, escrow, net_amount: float):
    """An automatic payout (transfer or gateway split) has paid the vendor:
    write the matching DEBIT so the ledger credit cannot be withdrawn again."""
    _debit_vendor_ledger(
        vendor_id=order.vendor_id,
        amount=net_amount,
        reference_id=payout_debit_reference(order, escrow),
        description=f"Automated gateway payout settlement for Order #{order.id}",
    )


def _payout_unless_split(order, escrow, gateway: str, tag: str) -> bool:
    """Pay the seller's share unless the gateway PROVABLY already did at
    checkout. True = the seller has been paid (proven split, or a transfer the
    gateway accepted and that is now tracked to its final status); False = the
    money stays in their Siiqo balance to withdraw. "Already paid" is decided
    only by app/services/split_settlement.py from the gateway's verified
    payment, never by a "SPLIT:" marker or the seller having a subaccount.
    Never both a split and a transfer."""
    from app.services.split_settlement import split_decision
    decision = split_decision(order, escrow, gateway)
    if decision == "split":
        logging.info(f"[{tag}] Order #{order.id}: {gateway} split proven ({escrow.split_proof}). No transfer.")
        return True
    if decision == "unknown":
        # A Paystack split may have paid them and Paystack couldn't be asked:
        # sending a transfer could pay twice. Keep it in their balance.
        logging.error(f"[{tag}] Order #{order.id}: split could not be checked; payout kept in balance")
        return False
    if gateway == "paystack":
        return bool(_paystack_payout_vendor(order, escrow))
    if gateway == "flutterwave":
        return bool(_flutterwave_payout_vendor(order, escrow))
    return False


PAYOUT_FAILED_TITLE = "Payout didn't go through"


def _notify_payout_failed(order, amount_ngn) -> None:
    """Tell the seller once per order that the automatic bank transfer failed
    and the money is safe in their balance. Not for deliberate holds."""
    try:
        exists = Notification.query.filter_by(
            user_id=order.vendor_id, order_id=order.id, title=PAYOUT_FAILED_TITLE
        ).first()
        if exists:
            return
        msg = (f"We couldn't send ₦{float(amount_ngn):,.2f} for Order #{order.id} to your bank "
               "automatically. It's safe in your Siiqo balance. Withdraw it from Payouts.")
        db.session.add(Notification(
            user_id=order.vendor_id, title=PAYOUT_FAILED_TITLE, message=msg,
            type="ESCROW", order_id=order.id,
        ))
        try:
            from app.models.user import User
            from app.utils.email import send_siiqo_email
            vendor_user = db.session.get(User, order.vendor_id)
            if vendor_user and vendor_user.email:
                send_siiqo_email(
                    to_email=vendor_user.email,
                    subject=f"Your payout for Order #{order.id} is in your Siiqo balance",
                    template_name="base_notification",
                    first_name=vendor_user.first_name or "there",
                    title=PAYOUT_FAILED_TITLE,
                    message=msg,
                    cta_text="Go to Payouts",
                    cta_url="https://siiqo.com/vendor/escrow",
                )
        except Exception as _em:  # noqa: BLE001
            logging.info(f"[PAYOUT] Failed-payout email not sent for Order #{order.id}: {_em}")
    except Exception as exc:  # noqa: BLE001
        logging.warning(f"[PAYOUT] Could not record failed-payout notice for Order #{order.id}: {exc}")


from app.services.escrow import get_escrow_provider
from app.services.fees import PLATFORM_FEE_PERCENT, platform_fee_float


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

    def _abs_url(u):
        if not u:
            return ""
        u_str = str(u).strip()
        if u_str.startswith('/static/'):
            base = os.environ.get('SITE_URL', 'https://siiqo.com').rstrip('/')
            try:
                from flask import request
                if request and hasattr(request, 'host_url') and request.host_url:
                    base = request.host_url.rstrip('/')
            except Exception:
                pass
            return f"{base}{u_str}"
        return u_str

    digital_items = []
    for item in (order.items or []):
        p = db.session.get(Prod, item.product_id) if item.product_id else item.product
        if p and (getattr(p, 'product_type', '') or '').strip().lower() == 'digital':
            digital_items.append((p, item))

    if not digital_items:
        return False

    if _dispute_blocks_payout(order, escrow):
        # Handled = nothing to do: the payment stays frozen while disputed.
        logging.warning(f"[DIGITAL] Order #{order.id} is disputed — delivery release skipped")
        return True

    # Build download list for email — HTML links so they are clickable in inbox
    download_links_html = "".join(
        f'<p style="margin:8px 0;"><strong>{p.name}</strong><br>'
        f'<a href="{_abs_url(p.file_url)}" style="color:#E0921C;font-weight:700;word-break:break-all;">{_abs_url(p.file_url)}</a></p>'
        for p, _ in digital_items if p.file_url
    ) or "<p>The vendor will share your download link shortly via Siiqo chat.</p>"

    # Release escrow immediately — no physical delivery required
    net_amount = _vendor_net_amount(order, escrow)
    escrow.status = EscrowStatus.RELEASED
    escrow.released_at = _utcnow()
    order.status = 'COMPLETED'

    # If paid via Paystack, handle vendor payout:
    # - If vendor has a subaccount_code → Paystack Split Payment already settled
    #   them directly at checkout. No manual transfer needed.
    # - Otherwise → fall back to manual Paystack Transfer (legacy flow).
    # NOTE: CRYPTO orders skip all Paystack payout — funds sit in Siiqo's Daya
    #       merchant balance and are paid out to the vendor separately via Daya.
    is_crypto = (order.payment_method or '').upper() in ('CRYPTO', 'DAYA_BANK_TRANSFER', 'DAYA')
    # A Daya order is paid out by the Daya confirmation handler, never here.
    from app.services.fx import is_flutterwave_paid
    is_flutterwave = not is_crypto and is_flutterwave_paid(order, escrow)
    # Use payment_method exclusively — the old payscrow_transaction_id heuristic
    # incorrectly flagged FLW orders as Paystack.
    is_paystack = (
        not is_crypto and not is_flutterwave
        and (order.payment_method or '').upper() in ('PAYSTACK', 'ESCROW')
    )
    dispatched = False
    if is_paystack:
        dispatched = _payout_unless_split(order, escrow, "paystack", "DIGITAL")
    elif is_flutterwave:
        dispatched = _payout_unless_split(order, escrow, "flutterwave", "DIGITAL")

    _credit_vendor_ledger(
        vendor_id=order.vendor_id,
        amount=net_amount,
        reference_id=escrow.transaction_number,
        description=f"Auto-released payout for digital Order #{order.id}",
    )
    if dispatched:
        _record_payout_debit(order, escrow, net_amount)

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

    # Notify vendor — include payout timing based on payment method
    vendor = db.session.get(User, order.vendor_id)
    if vendor and vendor.email:
        try:
            _pmethod = (order.payment_method or '').upper()
            _is_flw = is_flutterwave
            _is_psk = is_paystack
            if _is_flw:
                payout_line = (
                    f"💳 Payment method: Flutterwave (card/international).<br>"
                    f"Your bank settlement will arrive within <strong>24–48 hours</strong> after order completion — "
                    f"this is Flutterwave's standard T+1 settlement schedule."
                )
            elif _is_psk:
                payout_line = (
                    f"💳 Payment method: Paystack (card/USSD).<br>"
                    f"Your bank settlement will arrive within <strong>24–48 hours</strong> after order completion — "
                    f"this is Paystack's standard T+1 settlement schedule."
                )
            else:
                payout_line = (
                    f"⚡ Payment method: Bank Transfer / Crypto (Daya).<br>"
                    f"Your payout has been <strong>triggered immediately</strong> and should arrive in your bank account shortly."
                )
            send_siiqo_email(
                to_email=vendor.email,
                subject=f"Digital Sale Complete – Order #{order.id} | Siiqo",
                template_name="system_notice",
                first_name=vendor.first_name or "Vendor",
                notice_text=(
                    f"Your digital product was purchased and the download link has been delivered to the buyer automatically.<br><br>"
                    f"<strong>Order #{order.id}</strong> — ₦{net_amount:,.2f} credited to your account.<br><br>"
                    f"{payout_line}<br><br>"
                    f"No action needed from you — the order is fully complete. "
                    f"<a href='https://siiqo.com/vendor/orders'>View your order dashboard →</a>"
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

    def _abs_url(u):
        if not u:
            return ""
        u_str = str(u).strip()
        if u_str.startswith('/static/'):
            base = os.environ.get('SITE_URL', 'https://siiqo.com').rstrip('/')
            try:
                from flask import request
                if request and hasattr(request, 'host_url') and request.host_url:
                    base = request.host_url.rstrip('/')
            except Exception:
                pass
            return f"{base}{u_str}"
        return u_str

    service_items = []
    for item in (order.items or []):
        p = db.session.get(Prod, item.product_id) if item.product_id else item.product
        if p and (getattr(p, 'product_type', '') or '').strip().lower() == 'service':
            service_items.append((p, item))

    if not service_items:
        return False

    if _dispute_blocks_payout(order, escrow):
        logging.warning(f"[SERVICE] Order #{order.id} is disputed — delivery release skipped")
        return True

    # Build booking list for email — HTML links so they are clickable in inbox
    booking_links_html = "".join(
        f'<p style="margin:8px 0;"><strong>{p.name}</strong><br>'
        f'<a href="{_abs_url(p.booking_link)}" style="color:#E0921C;font-weight:700;word-break:break-all;">{_abs_url(p.booking_link)}</a></p>'
        for p, _ in service_items if p.booking_link
    ) or "<p>The vendor will reach out to you via Siiqo chat to schedule your service.</p>"

    # Release escrow immediately
    net_amount = _vendor_net_amount(order, escrow)
    escrow.status = EscrowStatus.RELEASED
    escrow.released_at = _utcnow()
    order.status = 'COMPLETED'

    # If paid via Paystack, handle vendor payout:
    # - If vendor has a subaccount_code → Paystack Split Payment already settled
    #   them directly at checkout. No manual transfer needed.
    # - Otherwise → fall back to manual Paystack Transfer (legacy flow).
    # NOTE: CRYPTO orders skip all Paystack payout (funds held in Daya balance).
    is_crypto = (order.payment_method or '').upper() in ('CRYPTO', 'DAYA_BANK_TRANSFER', 'DAYA')
    # A Daya order is paid out by the Daya confirmation handler, never here.
    from app.services.fx import is_flutterwave_paid
    is_flutterwave = not is_crypto and is_flutterwave_paid(order, escrow)
    is_paystack = (
        not is_crypto and not is_flutterwave
        and (order.payment_method or '').upper() in ('PAYSTACK', 'ESCROW')
    )
    dispatched = False
    if is_paystack:
        dispatched = _payout_unless_split(order, escrow, "paystack", "SERVICE")
    elif is_flutterwave:
        dispatched = _payout_unless_split(order, escrow, "flutterwave", "SERVICE")

    _credit_vendor_ledger(
        vendor_id=order.vendor_id,
        amount=net_amount,
        reference_id=escrow.transaction_number,
        description=f"Auto-released payout for service Order #{order.id}",
    )
    if dispatched:
        _record_payout_debit(order, escrow, net_amount)

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

    # Notify vendor — include payout timing based on payment method
    vendor = db.session.get(User, order.vendor_id)
    if vendor and vendor.email:
        try:
            _is_flw = is_flutterwave
            _is_psk = is_paystack
            if _is_flw:
                payout_line = (
                    f"💳 Payment method: Flutterwave (card/international).<br>"
                    f"Your bank settlement will arrive within <strong>24–48 hours</strong> after order completion — "
                    f"this is Flutterwave's standard T+1 settlement schedule."
                )
            elif _is_psk:
                payout_line = (
                    f"💳 Payment method: Paystack (card/USSD).<br>"
                    f"Your bank settlement will arrive within <strong>24–48 hours</strong> after order completion — "
                    f"this is Paystack's standard T+1 settlement schedule."
                )
            else:
                payout_line = (
                    f"⚡ Payment method: Bank Transfer / Crypto (Daya).<br>"
                    f"Your payout has been <strong>triggered immediately</strong> and should arrive in your bank account shortly."
                )
            send_siiqo_email(
                to_email=vendor.email,
                subject=f"Service Booking Sale – Order #{order.id} | Siiqo",
                template_name="system_notice",
                first_name=vendor.first_name or "Vendor",
                notice_text=(
                    f"Your service product was purchased and the booking link has been delivered to the buyer automatically.<br><br>"
                    f"<strong>Order #{order.id}</strong> — ₦{net_amount:,.2f} credited to your account.<br><br>"
                    f"{payout_line}<br><br>"
                    f"No action needed from you — the order is fully complete. "
                    f"<a href='https://siiqo.com/vendor/orders'>View your order dashboard →</a>"
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

    if _dispute_blocks_payout(order, escrow):
        logging.warning(f"[EVENT] Order #{order.id} is disputed — ticket release skipped")
        return True

    # Activate tickets (switches status PENDING -> ACTIVE, updates sold counts, emails buyer QR code)
    activate_tickets_for_order(order.id)

    # Release escrow immediately — events are instant
    net_amount = _vendor_net_amount(order, escrow)
    escrow.status = EscrowStatus.RELEASED
    escrow.released_at = _utcnow()
    order.status = 'COMPLETED'

    is_crypto = (order.payment_method or '').upper() in ('CRYPTO', 'DAYA_BANK_TRANSFER', 'DAYA')
    # A Daya order is paid out by the Daya confirmation handler, never here.
    from app.services.fx import is_flutterwave_paid
    is_flutterwave = not is_crypto and is_flutterwave_paid(order, escrow)
    is_paystack = (
        not is_crypto and not is_flutterwave
        and (order.payment_method or '').upper() in ('PAYSTACK', 'ESCROW')
    )
    dispatched = False
    if is_paystack:
        dispatched = _payout_unless_split(order, escrow, "paystack", "EVENT")
    elif is_flutterwave:
        dispatched = _payout_unless_split(order, escrow, "flutterwave", "EVENT")
    elif is_crypto:
        try:
            from app.routes.payments import _payout_vendor_via_daya
            dispatched = bool(_payout_vendor_via_daya(order, escrow))
        except Exception as _e:
            logging.warning(f"[EVENT] Daya payout failed for Order #{order.id}: {_e}")

    _credit_vendor_ledger(
        vendor_id=order.vendor_id,
        amount=net_amount,
        reference_id=escrow.transaction_number,
        description=f"Auto-released payout for Event Ticket Order #{order.id}",
    )
    if dispatched:
        _record_payout_debit(order, escrow, net_amount)

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

_DAYA_PAYMENT_METHODS = ('CRYPTO', 'DAYA', 'DAYA_BANK_TRANSFER', 'USDT', 'USDC')


def record_card_rail(order, rail: str) -> None:
    """A card gateway (`rail`, e.g. 'PAYSTACK') confirmed this order's payment.
    Orders created before the buyer picked a rail (event tickets, store
    checkout) can carry a Daya method; store the rail actually used so every
    release path pays out on it, never through Daya. 'ESCROW' (Paystack Pay
    Links and cart) is kept as it is."""
    if order is not None and (order.payment_method or '').upper() in _DAYA_PAYMENT_METHODS:
        logging.info(f"[RAIL] Order #{order.id} paid by {rail}; payment_method "
                     f"{order.payment_method} -> {rail}")
        order.payment_method = rail


def _activate_paystack_escrow(escrow, ref, txn=None) -> bool:
    """Mark one escrow paid after Paystack confirmed its payment, and deliver.
    Idempotent: only an escrow still PENDING_PAYMENT is touched. ``txn`` is
    the verified Paystack transaction: it decides whether a requested split
    really paid the seller (app/services/split_settlement.py)."""
    escrow = lock_escrow(escrow)  # a webhook and a status poll may race here
    if escrow is None or escrow.status != EscrowStatus.PENDING_PAYMENT:
        return False
    escrow.status = EscrowStatus.IN_ESCROW
    escrow.paid_at = escrow.paid_at or _utcnow()
    escrow.payscrow_transaction_id = ref
    order = escrow.order
    if not order:
        return True
    order.status = 'PAID'
    record_card_rail(order, 'PAYSTACK')
    from app.services.split_settlement import record_at_confirmation
    record_at_confirmation(order, escrow, "paystack", txn)
    db.session.flush()
    from app.services.fx import capture_for_order
    capture_for_order(order, escrow, stage="payment")

    is_event = _deliver_event_tickets(order, escrow)
    is_digital = not is_event and _deliver_digital_products(order, escrow)
    is_service = not is_event and not is_digital and _deliver_service_products(order, escrow)
    if not is_event and not is_digital and not is_service and order.payment_link_id:
        from app.models.payment_link import PaymentLink as _PL
        link = db.session.get(_PL, order.payment_link_id)
        link_ptype = getattr(link, 'product_type', 'service') or 'service'
        if link_ptype in ('digital', 'service'):
            net_amount = _vendor_net_amount(order, escrow)
            escrow.status = EscrowStatus.RELEASED
            escrow.released_at = _utcnow()
            order.status = 'COMPLETED'
            if link and link.link_type == 'INVOICE':
                link.status = 'PAID'
            _credit_vendor_ledger(
                vendor_id=order.vendor_id,
                amount=net_amount,
                reference_id=escrow.transaction_number,
                description=f"Paystack payout for Pay Link Order #{order.id}",
            )
            if _payout_unless_split(order, escrow, "paystack", "PAYLINK"):
                _record_payout_debit(order, escrow, net_amount)
    return True


def _activate_paystack_payment(ref, fallback=None, txn=None) -> int:
    """Paystack confirmed payment ``ref``: activate EVERY order it covered
    (app/services/payment_refs.py). Returns how many were activated now."""
    from app.services.payment_refs import escrows_for_payment
    escrows = escrows_for_payment(ref)
    if not escrows and fallback is not None:
        escrows = [fallback]
    return sum(1 for e in escrows if _activate_paystack_escrow(e, ref, txn=txn))


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
    # Only an unpaid order can be (re)initiated: never move a paid order
    # back to PENDING_PAYMENT or point it at a new payment.
    if any(e.status != EscrowStatus.PENDING_PAYMENT for e in escrow_txns):
        return jsonify({"message": "One of these orders is already paid."}), 409

    from app.services.payment_refs import escrow_txn_number, reusable_payment_ref
    existing_txn_number = reusable_payment_ref(escrow_txns, [o.id for o in orders])
    if existing_txn_number and existing_txn_number.startswith("FLW-"):
        existing_txn_number = None  # a Flutterwave attempt; Paystack gets its own reference
    # A retry reuses the earlier reference, so it is the same gateway payment.
    # Keep its split decision: if it was not split, never attach a split now
    # (the seller would be paid by the gateway AND by Siiqo's transfer).
    prior_split = None
    if existing_txn_number:
        prior_split = all(bool(e.split_requested) or e.split_settled is True for e in escrow_txns)

    # Delegate logic to Escrow Service Provider (Unified Payment)
    provider = get_escrow_provider(orders)
    if prior_split is False:
        result = provider.initiate_transaction(orders, existing_txn_number, allow_split=False)
    else:
        result = provider.initiate_transaction(orders, existing_txn_number)

    if not result.get("success"):
        return jsonify({"message": result.get("error_message") or "Escrow init failed"}), 400

    txn_map = {e.order_id: e for e in escrow_txns}
    # Only what was REQUESTED is recorded here: the subaccount a split was
    # asked for. Whether the split really paid the seller is decided at
    # payment confirmation from Paystack's verified transaction
    # (app/services/split_settlement.py record_at_confirmation).
    requested = {int(k): v for k, v in (result.get("split_subaccounts") or {}).items() if v} \
        if result.get("used_split") else {}
    if prior_split is True and not requested:
        # Same payment reference as a split attempt: keep what was asked so
        # confirmation checks Paystack for it (never assumed either way).
        logging.error(f"[ESCROW INIT] Retry of split payment {existing_txn_number} came back without a split")
        requested = {e.order.vendor_id: e.split_requested or "requested" for e in escrow_txns if e.order}

    payment_ref = result['transaction_number']
    for order in orders:
        escrow_txn = txn_map.get(order.id)
        txn_number = escrow_txn_number(payment_ref, order.id, len(orders))
        if not escrow_txn:
            # fee_amount for an individual order in a master transaction
            # the result['fee_amount'] is total, so we calculate proportional
            individual_fee = platform_fee_float(order.total_amount)
            escrow_txn = EscrowTransaction(
                order_id=order.id,
                transaction_number=txn_number,
                payment_ref=payment_ref,
                status=EscrowStatus.PENDING_PAYMENT,
                amount=float(order.total_amount),
                fee_percent=float(PLATFORM_FEE_PERCENT),
                fee_amount=individual_fee,
                payment_link=result['payment_link'],
                payscrow_transaction_id=result['provider_transaction_id'],
                payscrow_ref=result['provider_reference'],
                split_requested=(requested.get(order.vendor_id) or None),
                split_settled=None,
            )
            db.session.add(escrow_txn)
        else:
            escrow_txn.transaction_number = txn_number
            escrow_txn.payment_ref = payment_ref
            escrow_txn.payment_link = result['payment_link']
            escrow_txn.payscrow_transaction_id = result['provider_transaction_id']
            escrow_txn.payscrow_ref = result['provider_reference']
            escrow_txn.split_requested = requested.get(order.vendor_id) or None
            escrow_txn.split_settled = None
            escrow_txn.split_proof = None

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
    tx_ref_param = request.args.get('tx_ref')
    order_id_param = request.args.get('order_id') or request.args.get('ref')
    tx_id = request.args.get("transaction_id") or request.args.get("tx_id")

    escrow = None

    # 1. Direct query by numeric order_id
    if order_id_param and str(order_id_param).isdigit():
        escrow = EscrowTransaction.query.filter_by(order_id=int(order_id_param)).first()

    # 2. Query by explicit tx_ref (Flutterwave full reference)
    from app.services.payment_refs import escrows_for_payment
    if not escrow and tx_ref_param:
        escrow = next(iter(escrows_for_payment(tx_ref_param)), None)
        if not escrow and str(tx_ref_param).startswith("FLW-ORD-"):
            parts = str(tx_ref_param).split("-")
            if len(parts) >= 3 and parts[2].isdigit():
                escrow = EscrowTransaction.query.filter_by(order_id=int(parts[2])).first()
        if not escrow and str(tx_ref_param).startswith("FLW-PL-"):
            parts = str(tx_ref_param).split("-")
            if len(parts) >= 3 and parts[2].isdigit():
                escrow = EscrowTransaction.query.filter_by(order_id=int(parts[2])).first()

    # 3. Query by txn_param (exact, numeric, FLW-ORD-, PL-, or prefix)
    if not escrow and txn_param:
        escrow = next(iter(escrows_for_payment(txn_param)), None)
        if not escrow and str(txn_param).isdigit():
            escrow = EscrowTransaction.query.filter_by(order_id=int(txn_param)).first()
        if not escrow and str(txn_param).startswith("FLW-ORD-"):
            parts = str(txn_param).split("-")
            if len(parts) >= 3 and parts[2].isdigit():
                escrow = EscrowTransaction.query.filter_by(order_id=int(parts[2])).first()
        if not escrow and str(txn_param).startswith("PL-"):
            parts = str(txn_param).split("-")
            if len(parts) >= 2 and parts[1].isdigit():
                escrow = EscrowTransaction.query.filter_by(order_id=int(parts[1])).first()
        if not escrow and str(txn_param).startswith("FLW-PL-"):
            parts = str(txn_param).split("-")
            if len(parts) >= 3 and parts[2].isdigit():
                escrow = EscrowTransaction.query.filter_by(order_id=int(parts[2])).first()
        if not escrow:
            escrow = EscrowTransaction.query.filter(
                (EscrowTransaction.payscrow_transaction_id == txn_param) |
                (EscrowTransaction.payscrow_ref == txn_param)
            ).first()
        if not escrow:
            escrow = EscrowTransaction.query.filter(EscrowTransaction.transaction_number.like(f"{txn_param}%")).first()

    # 4. Fallback lookup via Flutterwave API if transaction_id is passed
    if not escrow and tx_id:
        try:
            from app.services import flutterwave_service as _flw
            v = _flw.verify_transaction(tx_id)
            if v.get("success"):
                flw_oid = v.get("order_id")
                flw_ref = v.get("tx_ref")
                if flw_oid and str(flw_oid).isdigit():
                    escrow = EscrowTransaction.query.filter_by(order_id=int(flw_oid)).first()
                if not escrow and flw_ref:
                    escrow = next(iter(escrows_for_payment(flw_ref)), None)
        except Exception as _flw_err:
            logging.warning(f"[ESCROW STATUS] Flutterwave lookup fallback failed: {_flw_err}")

    if not escrow:
        return jsonify({"message": "Transaction not found"}), 404

    # If escrow is still PENDING_PAYMENT, proactively verify with Flutterwave, Daya, or Paystack
    if escrow.status == EscrowStatus.PENDING_PAYMENT and escrow.transaction_number:
        if escrow.transaction_number.startswith("FLW-") or (escrow.order and escrow.order.payment_method == "FLUTTERWAVE"):
            try:
                from app.routes.payments import _handle_flutterwave_payment_confirmed
                from app.services import flutterwave_service as _flw
                lookup_tx_id = tx_id or request.args.get("transaction_id") or request.args.get("tx_id")
                if lookup_tx_id:
                    verif = _flw.verify_transaction(lookup_tx_id)
                    from app.routes.payments import flutterwave_verification_matches
                    if flutterwave_verification_matches(escrow, verif):
                        from app.services.payment_refs import gateway_ref
                        _handle_flutterwave_payment_confirmed(gateway_ref(escrow), str(lookup_tx_id), verif)
                        db.session.refresh(escrow)
            except Exception as _flw_err:
                logging.warning(f"[ESCROW STATUS] Flutterwave proactive verification failed: {_flw_err}")
        elif escrow.order and (escrow.order.payment_method or '').upper() in ('CRYPTO', 'DAYA_BANK_TRANSFER', 'DAYA'):
            try:
                from app.models.withdrawal import DayaPayment
                from app.routes.payments import _handle_crypto_payment_confirmed, _key_configured, apply_daya_deposit
                from app.services import daya_service
                dp = DayaPayment.query.filter_by(order_id=escrow.order.id).first()
                if dp and dp.daya_funding_account_id and _key_configured():
                    deposit = daya_service.get_deposit_by_funding_account(dp.daya_funding_account_id)
                    if deposit:
                        dep_fa = deposit.get("funding_account_id") or deposit.get("fundingAccountId") or ""
                        if dep_fa == dp.daya_funding_account_id:
                            # Settled deposit with the full amount only.
                            apply_daya_deposit(dp, deposit)
                            db.session.refresh(escrow)
                elif dp and dp.status == "COMPLETED":
                    _handle_crypto_payment_confirmed(escrow.order.id, dp)
                    db.session.refresh(escrow)
            except Exception as _daya_err:
                logging.warning(f"[ESCROW STATUS] Daya proactive verification failed: {_daya_err}")
        else:
            try:
                from app.services.escrow.paystack_provider import PaystackProvider
                from app.services.payment_refs import gateway_ref
                ref = gateway_ref(escrow)
                verification = PaystackProvider().verify_transaction(ref)
                if verification.get("success"):
                    # One payment can cover several orders: activate them all.
                    _activate_paystack_payment(ref, fallback=escrow, txn=verification.get("raw"))
                    db.session.commit()
                    db.session.refresh(escrow)
                    logging.info(f"[ESCROW STATUS] Proactively verified & activated payment {ref}")
            except Exception as _sync_err:
                logging.warning(f"[ESCROW STATUS] Paystack verification check failed: {_sync_err}")

    return jsonify(escrow.to_dict(include_private=_can_see_private(escrow, txn_param, tx_ref_param))), 200


def _can_see_private(escrow, *references) -> bool:
    """This endpoint is public. Private escrow fields go to the signed-in buyer
    or an admin, or to a caller holding the payment reference (the payer gets
    it from the gateway redirect). An order number alone is not enough."""
    known = {r for r in (escrow.transaction_number, escrow.payscrow_transaction_id, escrow.payment_ref,
                         (escrow.payscrow_ref or "").replace("SPLIT:", "")) if r}
    if any(ref and ref in known for ref in references):
        return True
    return _is_buyer_or_admin(get_jwt_identity(), escrow.order)


def _is_buyer_or_admin(identity, order) -> bool:
    if not identity or not order:
        return False
    try:
        uid = int(identity)
    except (TypeError, ValueError):
        return False
    if uid == order.buyer_id:
        return True
    from app.models.user import User, UserRole
    u = db.session.get(User, uid)
    return bool(u and u.role == UserRole.ADMIN)


# ---------------------------------------------------------------------------
# POST /escrow/webhook  — Legacy webhook endpoint (Payscrow removed)
#
# Payscrow has been fully removed. This endpoint is kept as a no-op so any
# lingering webhook pings from old payment links don't cause 404 errors.
# Active payment confirmations are handled by:
#   - Flutterwave:  POST /payments/flutterwave/webhook  (payments.py)
#   - Paystack:     POST /payments/webhook              (bridge.py)
#   - Daya:         POST /payments/daya/webhook         (payments.py)
# ---------------------------------------------------------------------------

@escrow_bp.route('/webhook', methods=['POST'])
def payscrow_webhook():
    """Legacy Payscrow webhook — no-op. Payscrow removed; all payment webhooks handled elsewhere."""
    logging.info("[LEGACY WEBHOOK] /escrow/webhook called — no active provider, returning 200.")
    return jsonify({"status": "ok", "message": "No active provider for this webhook endpoint."}), 200


def _paystack_payout_vendor(order, escrow):
    """
    Push vendor's net share via Paystack Transfers API.
    Used for instant payouts of digital/service orders or upon buyer delivery confirmation.
    """
    if _dispute_blocks_payout(order, escrow):
        logging.warning(f"[PAYSTACK TRANSFER] Refused for Order #{order.id}: dispute open")
        return False
    net_amount = _payout_amount_or_hold(order, escrow)
    if net_amount is None:
        return False
    if _verification_holds_payout(order, escrow, net_amount):
        return False
    
    bank_acc = VendorBankAccount.query.filter_by(
        vendor_id=order.vendor_id, is_default=True
    ).first()
    if not bank_acc:
        bank_acc = VendorBankAccount.query.filter_by(
            vendor_id=order.vendor_id
        ).first()

    # Storefront fallback: bank details given at onboarding become a payout
    # account, name pending (the typed name, store name or "Vendor" is never
    # used); the name check below asks the bank before any money moves.
    if not bank_acc or not bank_acc.account_number or not bank_acc.bank_code:
        try:
            from app.services.payout_account_name import mark_pending, pending_account_from_storefront
            from app.models.user import Storefront
            if not bank_acc:
                bank_acc = pending_account_from_storefront(order.vendor_id)
            else:
                sf = Storefront.query.filter_by(vendor_id=order.vendor_id).first()
                if sf and sf.account_number and sf.bank_code:
                    bank_acc.bank_code = sf.bank_code
                    bank_acc.account_number = sf.account_number
                    mark_pending(bank_acc)
            if bank_acc is not None:
                db.session.commit()
                logging.info(f"[PAYSTACK FALLBACK] Synced bank account from storefront for vendor {order.vendor_id} (name pending)")
        except Exception as _sf_err:
            db.session.rollback()
            logging.warning(f"[PAYSTACK FALLBACK] Failed to sync bank from storefront: {_sf_err}")

    # Only pay an account whose holder name the bank confirmed. One lookup
    # now; still unconfirmed -> the money stays in the seller's balance.
    if bank_acc and bank_acc.account_number and bank_acc.bank_code:
        from app.services.payout_account_name import hold_if_unconfirmed
        if hold_if_unconfirmed(bank_acc, vendor_id=order.vendor_id, order_id=order.id,
                               amount_ngn=net_amount):
            return False

    if bank_acc:
        if not bank_acc.recipient_code and bank_acc.account_number and bank_acc.bank_code:
            # Self-healing payout: create Paystack transfer recipient on-the-fly
            try:
                from app.services.escrow.paystack_provider import ensure_paystack_transfer_recipient
                # The name was confirmed by the bank just above.
                rec_res = ensure_paystack_transfer_recipient(
                    account_number=bank_acc.account_number,
                    bank_code=bank_acc.bank_code,
                    account_name=bank_acc.account_name,
                    name_confirmed=True,
                )
                if rec_res.get("success"):
                    bank_acc.recipient_code = rec_res["recipient_code"]
                    db.session.commit()
                    logging.info(
                        f"[PAYSTACK SELF-HEALING] Auto-created recipient {bank_acc.recipient_code} "
                        f"for vendor {order.vendor_id} on Order #{order.id}"
                    )
            except Exception as _heal_exc:
                logging.warning(f"[PAYSTACK SELF-HEALING] Failed to auto-create recipient: {_heal_exc}")

        if bank_acc.recipient_code:
            from app.services.escrow.paystack_provider import paystack_transfer_to_vendor
            transfer_result = paystack_transfer_to_vendor(
                recipient_code=bank_acc.recipient_code,
                amount_ngn=net_amount,
                # Same order + same recipient = same reference: Paystack refuses
                # a duplicate reference, so a repeat call can't pay twice.
                reference=payout_transfer_reference("PAYOUT", order, bank_acc.recipient_code),
                reason=f"Siiqo payout for Order #{order.id}",
            )
            if not transfer_result.get("success"):
                logging.error(
                    f"[PAYSTACK TRANSFER] Failed for Order #{order.id}: "
                    f"{transfer_result.get('error_message')}"
                )
                _notify_payout_failed(order, net_amount)
                return False
            _track_order_transfer(order, escrow, "paystack",
                                  payout_transfer_reference("PAYOUT", order, bank_acc.recipient_code),
                                  net_amount, transfer_result.get("transfer_code"),
                                  transfer_result.get("status"))
            return True
        if bank_acc.account_number and bank_acc.bank_code:
            # Bank details exist but Paystack would not set up the recipient:
            # a gateway problem, not a missing bank account.
            logging.error(f"[PAYSTACK TRANSFER] No recipient for Order #{order.id}; payout not attempted")
            _notify_payout_failed(order, net_amount)
            return False

    logging.warning(
        f"[RELEASE] Vendor {order.vendor_id} has no valid bank account or recipient_code. "
        "Crediting ledger only — notifying vendor to add bank account."
    )
    # Notify vendor so money is not stuck silently
    try:
        from app.models.communication import Notification
        from app.models.user import User
        notif_msg = (
            f"Your order #{order.id} has been marked complete. "
            f"To receive your payout of ₦{net_amount:,.2f}, please add your bank account in "
            "Escrow & Payouts (/vendor/escrow). Contact support@siiqo.com if you need help."
        )
        db.session.add(Notification(
            user_id=order.vendor_id,
            title="Action Required: Add Bank Account for Payout",
            message=notif_msg,
            type="ESCROW",
            order_id=order.id,
        ))
        db.session.commit()
        vendor_user = getattr(order, 'vendor', None) or db.session.get(User, order.vendor_id)
        if vendor_user and vendor_user.email:
            try:
                from app.utils.email import send_siiqo_email
                send_siiqo_email(
                    to_email=vendor_user.email,
                    subject=f"Action Required: Add bank account to receive payout for Order #{order.id}",
                    template_name="base_notification",
                    first_name=vendor_user.first_name or "there",
                    title="Payout Pending — Add Bank Account",
                    message=notif_msg,
                    cta_text="Add Bank Details",
                    cta_url="https://siiqo.com/vendor/escrow",
                )
            except Exception as _em_err:
                logging.info(f"[EMAIL] Could not send payout bank alert email: {_em_err}")
    except Exception as _notif_exc:
        logging.warning(f"[PAYSTACK PAYOUT] Failed to send missing bank notification: {_notif_exc}")

    return False


def _flutterwave_payout_vendor(order, escrow):
    """
    Push vendor's net share via Flutterwave Transfers API.
    Used for instant payouts of digital/service/event orders or upon delivery confirmation
    when subaccount split was not applied at checkout (e.g. multi-vendor cart).
    """
    if _dispute_blocks_payout(order, escrow):
        logging.warning(f"[FLW PAYOUT] Refused for Order #{order.id}: dispute open")
        return False
    net_amount = _payout_amount_or_hold(order, escrow)
    if net_amount is None:
        return False
    if _verification_holds_payout(order, escrow, net_amount):
        return False
    bank_acc = VendorBankAccount.query.filter_by(
        vendor_id=order.vendor_id, is_default=True
    ).first() or VendorBankAccount.query.filter_by(
        vendor_id=order.vendor_id
    ).first()

    # Storefront fallback: bank details given at onboarding become a payout
    # account, name pending (the typed name, store name or "Vendor" is never
    # used); the name check below asks the bank before any money moves.
    if not bank_acc or not bank_acc.account_number or not bank_acc.bank_code:
        try:
            from app.services.payout_account_name import mark_pending, pending_account_from_storefront
            from app.models.user import Storefront
            if not bank_acc:
                bank_acc = pending_account_from_storefront(order.vendor_id)
            else:
                sf = Storefront.query.filter_by(vendor_id=order.vendor_id).first()
                if sf and sf.account_number and sf.bank_code:
                    bank_acc.bank_code = sf.bank_code
                    bank_acc.account_number = sf.account_number
                    mark_pending(bank_acc)
            if bank_acc is not None:
                db.session.commit()
                logging.info(f"[FLW FALLBACK] Synced bank account from storefront for vendor {order.vendor_id} (name pending)")
        except Exception as _sf_err:
            db.session.rollback()
            logging.warning(f"[FLW FALLBACK] Failed to sync bank from storefront: {_sf_err}")

    # Only pay an account whose holder name the bank confirmed. One lookup
    # now; still unconfirmed -> the money stays in the seller's balance.
    if bank_acc and bank_acc.account_number and bank_acc.bank_code:
        from app.services.payout_account_name import hold_if_unconfirmed
        if hold_if_unconfirmed(bank_acc, vendor_id=order.vendor_id, order_id=order.id,
                               amount_ngn=net_amount):
            return False

    if not bank_acc or not bank_acc.account_number or not bank_acc.bank_code:
        logging.warning(
            f"[FLW PAYOUT] Vendor {order.vendor_id} has no valid bank details for Order #{order.id}. Skipping transfer and notifying vendor."
        )
        try:
            from app.models.communication import Notification
            from app.models.user import User
            notif_msg = (
                f"Your order #{order.id} has been marked complete. "
                f"To receive your payout of ₦{net_amount:,.2f}, please add your bank account in "
                "Escrow & Payouts (/vendor/escrow). Contact support@siiqo.com if you need help."
            )
            db.session.add(Notification(
                user_id=order.vendor_id,
                title="Action Required: Add Bank Account for Payout",
                message=notif_msg,
                type="ESCROW",
                order_id=order.id,
            ))
            db.session.commit()
            vendor_user = getattr(order, 'vendor', None) or db.session.get(User, order.vendor_id)
            if vendor_user and vendor_user.email:
                try:
                    from app.utils.email import send_siiqo_email
                    send_siiqo_email(
                        to_email=vendor_user.email,
                        subject=f"Action Required: Add bank account to receive payout for Order #{order.id}",
                        template_name="base_notification",
                        first_name=vendor_user.first_name or "there",
                        title="Payout Pending — Add Bank Account",
                        message=notif_msg,
                        cta_text="Add Bank Details",
                        cta_url="https://siiqo.com/vendor/escrow",
                    )
                except Exception as _em_err:
                    logging.info(f"[EMAIL] Could not send FLW payout bank alert email: {_em_err}")
        except Exception as _notif_exc:
            logging.warning(f"[FLW PAYOUT] Failed to send missing bank notification: {_notif_exc}")

        return False

    try:
        from app.services import flutterwave_service as _flw
        # Deterministic per order + bank account: Flutterwave rejects a
        # duplicate reference, so a repeat call can't send a second transfer.
        payout_ref = payout_transfer_reference(
            "WD-FLW", order, f"{bank_acc.bank_code}:{bank_acc.account_number}")
        flw_tx = _flw.transfer_to_vendor(
            account_bank=bank_acc.bank_code,
            account_number=bank_acc.account_number,
            amount_ngn=net_amount,
            reference=payout_ref,
            narration=f"Siiqo payout for Order #{order.id}",
        )
        if flw_tx.get("success"):
            logging.info(f"[FLW PAYOUT] ✓ Flutterwave transfer dispatched for Order #{order.id}: ref={payout_ref}")
            _track_order_transfer(order, escrow, "flutterwave", payout_ref, net_amount,
                                  flw_tx.get("transfer_id"), flw_tx.get("status"))
            return True
        else:
            logging.warning(f"[FLW PAYOUT] Flutterwave transfer failed for Order #{order.id}: {flw_tx.get('error_message')}")
            _notify_payout_failed(order, net_amount)
            return False
    except Exception as exc:
        logging.error(f"[FLW PAYOUT] Flutterwave transfer error for Order #{order.id}: {exc}", exc_info=True)
        _notify_payout_failed(order, net_amount)
        return False



def execute_order_escrow_release(order, escrow, source="admin"):
    """
    Unified, idempotent order completion & payout pipeline.
    1. Sets escrow.status = RELEASED
    2. Credits vendor ledger balance
    3. Fires transfer payout (Paystack, Flutterwave, or Daya based on order gateway)
    4. Sets order.status = 'COMPLETED'
    5. Sends notifications to buyer & vendor
    """
    if not order or not escrow:
        return False

    # One release at a time per escrow: lock the row and re-read its status
    # (buyer, token, admin, auto-release and dispute decisions all come here).
    escrow = lock_escrow(escrow)
    if escrow is None:
        return False

    if escrow.status == EscrowStatus.RELEASED:
        order.status = 'COMPLETED'
        db.session.commit()
        return True

    # Dispute guard: a disputed escrow, an active dispute case, or money
    # already refunded/cancelled is never released here. A dispute decided
    # for the seller calls this function after the case is resolved.
    if escrow.status in (EscrowStatus.DISPUTED, EscrowStatus.REFUNDED, EscrowStatus.CANCELLED) \
            or _dispute_blocks_payout(order, escrow):
        logging.warning(
            f"[ESCROW RELEASE REFUSED] Order #{order.id} is disputed or closed "
            f"(escrow_status={escrow.status}, source={source})"
        )
        return False

    # Critical Guard: Never release funds or dispatch payouts on unpaid orders
    if escrow.status == EscrowStatus.PENDING_PAYMENT or order.status in ('PENDING', 'CANCELLED'):
        logging.critical(
            f"[ESCROW RELEASE REFUSED] Attempted to release unpaid escrow for Order #{order.id} "
            f"(escrow_status={escrow.status}, order_status={order.status}, source={source})"
        )
        return False

    net_amount = _vendor_net_amount(order, escrow)
    from app.services.delivery_money import credit_seller_delivery, payout_breakdown, seller_delivery_share
    delivery_share = float(seller_delivery_share(order, escrow))
    breakdown = payout_breakdown(order, escrow)
    escrow.status = EscrowStatus.RELEASED
    escrow.released_at = _utcnow()
    order.status = 'COMPLETED'

    # Two credits: the item share (existing reference) and, for the seller's
    # own delivery, the whole delivery fee under its fixed reference
    # delivery_fee:<order_id> (no 3% on it; idempotent).
    _credit_vendor_ledger(
        vendor_id=order.vendor_id,
        amount=round(net_amount - delivery_share, 2),
        reference_id=escrow.transaction_number or f"ORD-{order.id}",
        description=f"Payout for Order #{order.id} ({source})",
    )
    if delivery_share > 0:
        credit_seller_delivery(order, f"Your delivery fee for Order #{order.id} ({source})")

    # Determine payout channel: Daya (crypto/NGN bank-transfer onramp) vs Paystack vs Flutterwave
    from app.services.fx import is_daya_paid, is_flutterwave_paid
    is_daya_order = is_daya_paid(order, escrow)
    is_flutterwave_order = is_flutterwave_paid(order, escrow)  # never true for a Daya order

    payout_dispatched = False
    if is_flutterwave_order:
        payout_dispatched = _payout_unless_split(order, escrow, "flutterwave", "ESCROW RELEASE")
    elif is_daya_order:
        try:
            from app.routes.payments import _payout_vendor_via_daya
            payout_dispatched = bool(_payout_vendor_via_daya(order, escrow))
        except Exception as _d_err:
            logging.warning(f"[ESCROW RELEASE] Daya payout error for Order #{order.id}: {_d_err}")
    else:
        # Paystack channel
        payout_dispatched = _payout_unless_split(order, escrow, "paystack", "ESCROW RELEASE")

    # Write matching DEBIT to ledger if payout was settled via gateway or split,
    # preventing double-withdrawal from Siiqo corporate balance
    if payout_dispatched:
        _debit_vendor_ledger(
            vendor_id=order.vendor_id,
            amount=net_amount,
            reference_id=payout_debit_reference(order, escrow),
            description=f"Automated gateway payout settlement for Order #{order.id}",
        )

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
            message=(f"Payment of ₦{net_amount:,.2f} for Order #{order.id} has been released to your account."
                     + (f" {breakdown['text']}." if delivery_share > 0 else "")),
            type="ESCROW",
            order_id=order.id,
        ))
    except Exception as _notif_err:
        logging.warning(f"[ESCROW RELEASE] Notification warning: {_notif_err}")

    db.session.commit()

    try:
        from app.services.trust import recalculate_vendor_trust
        recalculate_vendor_trust(order.vendor_id, reason="Escrow Released")
    except Exception as e:
        logging.error(f"[TRUST ERROR] Failed to recalculate trust on escrow release: {e}")

    # ── Send emails to vendor and buyer (fires for ALL release paths: buyer release, token, admin) ──
    from app.utils.email import send_siiqo_email
    from app.models.user import User as _U

    vendor = db.session.get(_U, order.vendor_id)
    if vendor and vendor.email:
        try:
            send_siiqo_email(
                to_email=vendor.email,
                subject=f"Siiqo - Payout Released for Order #{order.id}",
                template_name="system_notice",
                first_name=vendor.first_name or "Vendor",
                notice_text=(
                    f"Great news! The buyer has confirmed delivery for Order #{order.id}.<br><br>"
                    f"<strong>₦{net_amount:,.2f}</strong> has been released to your account.<br><br>"
                    + (f"{_html.escape(breakdown['text'])}<br><br>" if delivery_share > 0 else "")
                    + f"<a href='https://siiqo.com/vendor/orders'>View your orders →</a>"
                ),
            )
        except Exception as e:
            logging.warning(f"[EMAIL WARN] payout release vendor email failed Order #{order.id}: {e}")

    buyer = db.session.get(_U, order.buyer_id) if order.buyer_id else None
    buyer_email = (buyer.email if buyer else None) or getattr(order, 'buyer_email', None)
    if buyer_email:
        buyer_name = (buyer.first_name if buyer else None) or getattr(order, 'buyer_name', None) or "Buyer"
        try:
            send_siiqo_email(
                to_email=buyer_email,
                subject=f"Siiqo - Order #{order.id} Completed",
                template_name="system_notice",
                first_name=buyer_name,
                notice_text=(
                    f"Thank you! Order #{order.id} is now complete and payment has been released to the vendor.<br><br>"
                    f"How was it? Rate {_html.escape(_store_name(order))} in one tap: "
                    f"<a href='{review_url(order.id)}'>{review_url(order.id)}</a>"
                ),
            )
        except Exception as e:
            logging.warning(f"[EMAIL WARN] order completed buyer email failed Order #{order.id}: {e}")

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

    escrow = None
    if txn_number:
        escrow = db.session.query(EscrowTransaction).filter_by(
            transaction_number=txn_number
        ).with_for_update().first()
    if not escrow and order_id:
        escrow = db.session.query(EscrowTransaction).filter_by(
            order_id=order_id
        ).with_for_update().first()
    if not txn_number and not order_id:
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
        # Try Paystack verify (active provider for cart checkout)
        try:
            from app.services.escrow.paystack_provider import PaystackProvider
            from app.services.payment_refs import escrows_for_payment, gateway_ref
            ref = gateway_ref(escrow)
            result = PaystackProvider().verify_transaction(ref)
            if result.get("success"):
                # Mark every order of this payment paid (status only, as before).
                for e in escrows_for_payment(ref) or [escrow]:
                    if e.status == EscrowStatus.PENDING_PAYMENT:
                        e.status = EscrowStatus.IN_ESCROW
                        e.paid_at = e.paid_at or _utcnow()
                        record_card_rail(e.order, 'PAYSTACK')
                db.session.commit()
        except Exception as e:
            logging.warning(f"[RELEASE] Paystack verify fallback failed: {e}")

    if escrow.status not in [
        EscrowStatus.IN_ESCROW, EscrowStatus.DELIVERED, EscrowStatus.SHIPPED
    ]:
        return jsonify({
            "message": f"Cannot release funds at status: {escrow.status}"
        }), 400

    # ── Delegate to unified release pipeline ─────────────────────────────────
    # execute_order_escrow_release is the single source of truth for:
    # state transitions, ledger tracking, gateway payouts, and notifications.
    execute_order_escrow_release(order, escrow, source="buyer_release")

    return jsonify({
        "success": True,
        "message": "Funds released to vendor successfully.",
        "net_amount": str(_vendor_net_amount(order, escrow)),
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

    # Opens a dispute case (evidence, deadlines, review): app/services/disputes.py.
    # This older endpoint still lets the seller raise one; the case then waits
    # for the buyer first.
    from app.services.disputes import DisputeError, open_case
    try:
        case = open_case(order, user_id, reason or "Other", data.get('details'), allow_seller=True)
    except DisputeError as e:
        return jsonify(e.to_dict()), (400 if e.code == "already_open" else e.status)

    return jsonify({
        "success": True,
        "disputeId": case.reference,
        "dispute_case_id": case.id,
        "message": "Dispute raised. The payment is on hold while both sides add evidence.",
    }), 200


# ---------------------------------------------------------------------------
# TOKEN-BASED GUEST DELIVERY CONFIRMATION
# Allows guest buyers to view order summary and confirm receipt in 1 click
# without requiring an account or login.
# ---------------------------------------------------------------------------

def generate_order_token(order_id: int) -> str:
    secret = (os.environ.get("SECRET_KEY") or "siiqo-secret-key-salt-2026").encode()
    return hmac.new(secret, f"order-confirm-{order_id}".encode(), hashlib.sha256).hexdigest()[:32]


def generate_review_token(order_id: int) -> str:
    """Private link token for reviewing an order without an account.
    Separate from the order token, so a review link can't confirm delivery."""
    secret = (os.environ.get("SECRET_KEY") or "siiqo-secret-key-salt-2026").encode()
    return hmac.new(secret, f"order-review-{order_id}".encode(), hashlib.sha256).hexdigest()[:32]


def review_token_valid(order_id, token) -> bool:
    token = (token or "").strip()
    if not token or order_id is None:
        return False
    return hmac.compare_digest(generate_review_token(int(order_id)), token)


def review_url(order_id: int) -> str:
    return f"https://siiqo.com/review/{order_id}?token={generate_review_token(order_id)}"


def review_block_reason(order):
    """None when the order can be reviewed now, else NOT_DELIVERED_YET or
    ALREADY_REVIEWED. Same gate for the logged-in and the review-link routes."""
    from app.models.community import Review
    order_status = (order.status or "").upper()
    escrow = EscrowTransaction.query.filter_by(order_id=order.id).first()
    escrow_status = (escrow.status or "").upper() if escrow else ""
    if order_status not in ('COMPLETED', 'DELIVERED') and escrow_status not in ('RELEASED', 'REFUNDED'):
        return "NOT_DELIVERED_YET"
    # One review per order, whoever wrote it (guest link or signed-in buyer).
    if Review.query.filter_by(order_id=order.id).first() is not None:
        return "ALREADY_REVIEWED"
    return None


def buyer_first_name(order):
    """First name only, for the review page and public reviews. Never the email."""
    name = None
    try:
        if order.buyer is not None:
            name = order.buyer.first_name
    except Exception:  # noqa: BLE001
        name = None
    if not name:
        name = getattr(order, "buyer_name", None)
    name = (name or "").strip()
    if not name or "@" in name:
        return None
    return name.split()[0][:80]


def _store_name(order):
    try:
        sf = order.vendor.storefront if order.vendor else None
        if sf and sf.store_name:
            return sf.store_name
    except Exception:  # noqa: BLE001
        pass
    return "the seller"


def _item_image(product):
    if product is None:
        return None
    for attr in ("image_url", "main_image", "thumbnail"):
        val = getattr(product, attr, None)
        if isinstance(val, str) and val:
            return val
    imgs = getattr(product, "images", None)
    if isinstance(imgs, (list, tuple)) and imgs:
        first = imgs[0]
        if isinstance(first, str):
            return first
        if isinstance(first, dict):
            return first.get("url") or first.get("image_url")
    return None


def _parse_rating(value):
    try:
        rating = int(value)
    except (TypeError, ValueError):
        return None
    if isinstance(value, float) and value != rating:
        return None
    return rating if 1 <= rating <= 5 else None


def create_order_review(order, vendor_rating, product_rating, review_text, buyer_id=None, reviewer_name=None):
    """Save a review, tell the seller and refresh their trust score.
    Shared by POST /reviews and POST /escrow/review-by-token."""
    from app.models.community import Review
    product_id = None
    for it in order.items:
        if it.product_id:
            product_id = it.product_id
            break
    review = Review(
        order_id=order.id,
        buyer_id=buyer_id,
        vendor_id=order.vendor_id,
        product_id=product_id,
        vendor_rating=vendor_rating,
        product_rating=product_rating,
        review_text=review_text or None,
        reviewer_name=reviewer_name,
    )
    db.session.add(review)
    db.session.add(Notification(
        user_id=order.vendor_id,
        title=f"New Review ({vendor_rating}/5 ⭐)",
        message=review_text or f"A buyer left a {vendor_rating}-star review for Order #{order.id}.",
        type="REVIEW",
        order_id=order.id,
    ))
    db.session.commit()
    try:
        from app.services.trust import recalculate_vendor_trust
        recalculate_vendor_trust(order.vendor_id, reason="Review Submitted")
    except Exception as e:  # noqa: BLE001
        logging.error(f"[TRUST ERROR] Failed to recalculate trust on review submit: {e}")
    return review


@escrow_bp.route('/review-by-token', methods=['GET'])
@limiter.limit("60 per hour")
def get_review_by_token():
    """What the guest review page shows. Needs the private review token."""
    order_id = request.args.get('order_id', type=int)
    token = request.args.get('token')
    order = db.session.get(Order, order_id) if order_id else None
    if not order or not review_token_valid(order_id, token):
        return jsonify({"message": "This review link is not valid."}), 404

    items = []
    for it in order.items:
        product = it.product
        items.append({
            "name": product.name if product else "Item",
            "image": _item_image(product),
        })
    reason = review_block_reason(order)
    return jsonify({
        "order_id": order.id,
        "store_name": _store_name(order),
        "items": items,
        "can_review": reason is None,
        "reason": reason,
        "buyer_first_name": buyer_first_name(order),
    }), 200


@escrow_bp.route('/review-by-token', methods=['POST'])
@limiter.limit("10 per hour")
def submit_review_by_token():
    """Leave a review from the private link in the order emails. No account needed."""
    data = request.get_json(silent=True) or {}
    try:
        order_id = int(data.get('order_id'))
    except (TypeError, ValueError):
        order_id = None
    order = db.session.get(Order, order_id) if order_id else None
    if not order or not review_token_valid(order_id, data.get('token')):
        return jsonify({"message": "This review link is not valid."}), 404

    vendor_rating = _parse_rating(data.get('vendor_rating'))
    if vendor_rating is None:
        return jsonify({"message": "Pick a rating from 1 to 5.", "code": "BAD_RATING"}), 400
    product_rating = None
    if data.get('product_rating') not in (None, ""):
        product_rating = _parse_rating(data.get('product_rating'))
        if product_rating is None:
            return jsonify({"message": "Pick a product rating from 1 to 5.", "code": "BAD_RATING"}), 400
    review_text = data.get('review') or data.get('review_text') or ''
    if not isinstance(review_text, str):
        return jsonify({"message": "Review must be text.", "code": "BAD_REVIEW"}), 400
    review_text = review_text.strip()
    if len(review_text) > 1000:
        return jsonify({"message": "Keep your review under 1000 characters.", "code": "REVIEW_TOO_LONG"}), 400

    reason = review_block_reason(order)
    if reason == "NOT_DELIVERED_YET":
        return jsonify({"message": "You can review this order once it has been delivered.",
                        "code": reason}), 400
    if reason == "ALREADY_REVIEWED":
        return jsonify({"message": "This order has already been reviewed. Thank you!", "code": reason}), 409

    create_order_review(order, vendor_rating, product_rating, review_text,
                        buyer_id=order.buyer_id, reviewer_name=buyer_first_name(order))
    return jsonify({"status": "success", "message": "Thank you for your review!"}), 201


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

        # A shared link (no token, not signed in) shows the order summary only;
        # the buyer then confirms with a code emailed to them.
        show_contact = token_valid or is_authorized_user

        vendor_name = "Vendor"
        try:
            if order.vendor and hasattr(order.vendor, 'storefront') and order.vendor.storefront:
                vendor_name = order.vendor.storefront.store_name
            elif order.vendor:
                vendor_name = order.vendor.first_name or "Vendor"
        except Exception:
            vendor_name = "Vendor"

        # Safe Pay deadline: after the seller marks it delivered, the buyer has
        # until this time to confirm or report a problem.
        from app.models.escrow import EscrowTransaction as _Escrow
        from app.services.trust import release_deadline
        from app.tasks.escrow_tasks import format_deadline
        escrow_row = _Escrow.query.filter_by(order_id=order.id).first()
        confirm_by = release_deadline(escrow_row)

        items_list = []
        for it in order.items:
            unit_price = getattr(it, 'price_at_purchase', getattr(it, 'price', 0))
            items_list.append({
                "name": it.product.name if it.product else "Item",
                "quantity": it.quantity,
                "price": float(unit_price or 0),
            })

        # What the website needs to resume an unpaid order, and the review link.
        from app.tasks.escrow_tasks import unpaid_cancel_at
        from app.services.delivery_money import delivery_by as _delivery_by
        escrow_status = escrow_row.status if escrow_row else None
        cancel_at = unpaid_cancel_at(order, escrow_row)
        is_order_buyer = False
        try:
            is_order_buyer = bool(user_id) and order.buyer_id is not None and int(user_id) == order.buyer_id
        except (TypeError, ValueError):
            is_order_buyer = False
        order_review_url = None
        if (token_valid or is_order_buyer) and review_block_reason(order) is None:
            order_review_url = review_url(order.id)
        resume = {
            "payment_method": order.payment_method,
            "escrow_status": escrow_status,
            "total_payable": round(float(order.total_amount or 0) + float(order.logistics_fee or 0), 2),
            # Item · Delivery (partner/seller) · Total. Siiqo's 3% is never on delivery.
            "delivery_fee": round(float(order.logistics_fee or 0), 2),
            "delivery_by": _delivery_by(order),
            "cancel_at": cancel_at.isoformat() if cancel_at else None,
            "is_guest_order": order.buyer_id is None,
            "review_url": order_review_url,
        }

        return jsonify({
            "status": "success",
            **resume,
            "order": {
                **resume,
                "id": order.id,
                "status": order.status,
                "total_amount": float(order.total_amount),
                "promo_discount_ngn": float(order.promo_discount_ngn) if order.promo_discount_ngn else None,
                "buyer_name": order.buyer_name if show_contact else None,
                "buyer_email": order.buyer_email if show_contact else None,
                "buyer_phone": order.buyer_phone if show_contact else None,
                "vendor_name": vendor_name,
                "created_at": order.created_at.isoformat() if order.created_at else None,
                "items": items_list,
                "confirm_by": confirm_by.isoformat() if confirm_by else None,
                "confirm_by_text": format_deadline(confirm_by) if confirm_by else None,
                "safe_pay": escrow_row is not None,
            },
            "verified_buyer": bool(token_valid or _is_buyer_or_admin(user_id, order)),
            "code_email_hint": _mask_email(_buyer_contact_email(order)),
        }), 200
    except Exception as e:
        import traceback
        logging.error(f"[ESCROW] Error loading order details for order #{order_id}: {e}\n{traceback.format_exc()}")
        return jsonify({"message": f"Could not load order #{order_id}."}), 500


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
    code = str(data.get('code') or '').strip()
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
        if not code:
            return jsonify({"message": "Only the buyer can confirm receipt. Request a code to your email to continue.",
                            "code": "BUYER_CODE_REQUIRED"}), 403
        ok, message = _check_delivery_code(order, code)
        if not ok:
            return jsonify({"message": message, "code": "BUYER_CODE_INVALID"}), 403

    escrow = EscrowTransaction.query.filter_by(order_id=order.id).first()
    if not escrow:
        return jsonify({"message": "No escrow record found for this order"}), 404

    if escrow.status == EscrowStatus.RELEASED or order.status == 'COMPLETED':
        return jsonify({
            "status": "success",
            "message": "Order is already completed and received. Thank you!",
            "already_completed": True,
        }), 200

    # Guard: Never release unpaid escrow via token
    if escrow.status == EscrowStatus.PENDING_PAYMENT or order.status not in ('PAID', 'SHIPPED', 'DELIVERED'):
        return jsonify({
            "message": "Order payment has not been confirmed yet. Cannot confirm receipt of an unpaid order.",
            "code": "PAYMENT_NOT_CONFIRMED",
        }), 400

    success = execute_order_escrow_release(order, escrow, source="guest_buyer_token")
    if not success:
        return jsonify({"message": "Could not complete release. Please contact support."}), 500

    return jsonify({
        "status": "success",
        "message": "Delivery confirmed! Payment has been released to the vendor. Thank you for shopping on Siiqo.",
    }), 200


# ---------------------------------------------------------------------------
# BUYER CODE FOR SHARED LINKS
# The seller may share the order link (e.g. on WhatsApp), but only the buyer
# can release escrow: confirming from a shared link needs a one-time code sent
# to the buyer's email on the order.
# ---------------------------------------------------------------------------

DELIVERY_CODE_TTL_MINUTES = 15
DELIVERY_CODE_MAX_ATTEMPTS = 5
DELIVERY_CODE_RESEND_SECONDS = 60


def _buyer_contact_email(order):
    email = ((order.buyer.email if order.buyer else None) or order.buyer_email or "").strip()
    # Pay Link guests without an email get a placeholder address; it can't receive codes.
    if not email or (email.startswith("guest-") and email.endswith("@siiqo.com")):
        return None
    return email


def _mask_email(email):
    if not email or "@" not in email:
        return None
    name, domain = email.split("@", 1)
    return f"{name[:1]}{'*' * max(len(name) - 1, 2)}@{domain}"


def _delivery_code_hash(order_id, code):
    secret = (os.environ.get("SECRET_KEY") or "siiqo-secret-key-salt-2026").encode()
    return hmac.new(secret, f"delivery-code-{order_id}-{code}".encode(), hashlib.sha256).hexdigest()


def _aware_dt(dt):
    return dt.replace(tzinfo=timezone.utc) if dt is not None and dt.tzinfo is None else dt


def _check_delivery_code(order, code):
    from app.models.escrow import DeliveryConfirmationCode
    row = DeliveryConfirmationCode.query.filter_by(order_id=order.id).with_for_update().first()
    if not row or _aware_dt(row.expires_at) < _utcnow():
        return False, "This code has expired. Request a new one."
    if row.attempts >= DELIVERY_CODE_MAX_ATTEMPTS:
        return False, "Too many wrong codes. Request a new one."
    if not hmac.compare_digest(row.code_hash, _delivery_code_hash(order.id, code)):
        row.attempts += 1
        db.session.commit()
        return False, "That code is not correct."
    db.session.delete(row)
    return True, None


@escrow_bp.route('/confirm-delivery/send-code', methods=['POST'])
@limiter.limit("5 per 15 minutes")
def send_delivery_code():
    """Email the buyer a one-time code to confirm delivery from a shared link."""
    from app.models.order import Order
    from app.models.escrow import DeliveryConfirmationCode
    from app.utils.email import send_siiqo_email

    data = request.get_json() or {}
    try:
        order_id = int(data.get('order_id'))
    except (TypeError, ValueError):
        return jsonify({"message": "order_id is required"}), 400
    order = db.session.get(Order, order_id)
    if not order:
        return jsonify({"message": "Order not found"}), 404

    email = _buyer_contact_email(order)
    if not email:
        return jsonify({
            "message": "This order has no buyer email. Use the link from your payment receipt, or sign in.",
            "code": "NO_BUYER_EMAIL",
        }), 409

    row = db.session.get(DeliveryConfirmationCode, order.id)
    now = _utcnow()
    if row and row.sent_at and (now - _aware_dt(row.sent_at)).total_seconds() < DELIVERY_CODE_RESEND_SECONDS:
        return jsonify({"message": "A code was just sent. Please wait a minute before asking again.",
                        "sent_to": _mask_email(email)}), 429

    import secrets
    code = f"{secrets.randbelow(1_000_000):06d}"
    if row is None:
        row = DeliveryConfirmationCode(order_id=order.id)
        db.session.add(row)
    row.code_hash = _delivery_code_hash(order.id, code)
    row.expires_at = now + timedelta(minutes=DELIVERY_CODE_TTL_MINUTES)
    row.attempts = 0
    row.sent_at = now
    db.session.commit()

    try:
        send_siiqo_email(
            to_email=email,
            subject=f"Your code to confirm Order #{order.id} | Siiqo",
            template_name="system_notice",
            first_name=(order.buyer.first_name if order.buyer else None) or order.buyer_name or "there",
            notice_text=(
                f"Your code to confirm you received Order #{order.id} is <strong>{code}</strong>.<br><br>"
                f"It expires in {DELIVERY_CODE_TTL_MINUTES} minutes. Only enter it once you have your order: "
                "confirming releases the payment to the seller. Never share this code with the seller."
            ),
        )
    except Exception as e:
        logging.warning(f"[DELIVERY CODE] Email failed for Order #{order.id}: {e}")
        return jsonify({"message": "We couldn't send the code. Please try again shortly."}), 502

    return jsonify({"status": "success", "sent_to": _mask_email(email)}), 200


# Dispute case routes for buyers and sellers (/api/escrow/disputes/...), Phase 2 item 2-5.
from app.routes import disputes as _dispute_routes  # noqa: E402,F401
