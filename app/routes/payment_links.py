import os
import uuid
import re
import logging
from datetime import datetime, timezone, timedelta
from decimal import Decimal

from flask import Blueprint, request, jsonify
from flask_jwt_extended import jwt_required, get_jwt_identity

from app.extensions import db
from app.models.user import User, Storefront, UserRole
from app.models.order import Order, OrderItem
from app.models.escrow import EscrowTransaction, EscrowStatus
from app.models.payment_link import PaymentLink
from app.models.withdrawal import VendorBankAccount

payment_links_bp = Blueprint('payment_links', __name__)

def _utcnow():
    return datetime.now(timezone.utc)

def _get_vendor(user_id) -> User | None:
    user = db.session.get(User, int(user_id))
    if not user:
        return None
    if user.role in [UserRole.VENDOR, UserRole.ADMIN] or user.storefront is not None:
        return user
    return None

# ===========================================================================
# VENDOR ENDPOINTS
# ===========================================================================

@payment_links_bp.route('/vendor/payment-links', methods=['POST'])
@jwt_required()
def create_payment_link():
    user_id = get_jwt_identity()
    vendor = _get_vendor(user_id)
    if not vendor:
        return jsonify({"message": "Vendor access required"}), 403
    if not vendor.storefront:
        return jsonify({"message": "Complete vendor onboarding first"}), 403

    data = request.get_json() or {}
    title = (data.get('title') or '').strip()
    description = (data.get('description') or '').strip()
    amount_str = data.get('amount')
    link_type = (data.get('link_type') or 'PAY_LINK').upper()
    buyer_email = (data.get('buyer_email') or '').strip().lower()
    product_type = (data.get('product_type') or 'service').lower()
    if product_type not in ('physical', 'digital', 'service'):
        product_type = 'service'

    if not title:
        return jsonify({"message": "Title is required"}), 400

    if link_type not in ['PAY_LINK', 'INVOICE']:
        return jsonify({"message": "Invalid link_type. Allowed: PAY_LINK, INVOICE"}), 400

    # For Invoice, amount is required
    amount = None
    if amount_str is not None:
        try:
            amount = Decimal(str(amount_str))
            if amount <= 0:
                return jsonify({"message": "Amount must be greater than zero"}), 400
        except Exception:
            return jsonify({"message": "Invalid amount format"}), 400
    elif link_type == 'INVOICE':
        return jsonify({"message": "Amount is required for invoice links"}), 400

    # Auto-generate a unique slug
    clean_title = re.sub(r'[^a-z0-9]+', '-', title.lower()).strip('-')
    slug = f"{clean_title}-{uuid.uuid4().hex[:6]}"
    # Fallback if title contains no letters/numbers
    if not slug or slug.startswith('-'):
        slug = f"pay-{uuid.uuid4().hex[:10]}"

    new_link = PaymentLink(
        vendor_id=vendor.id,
        link_type=link_type,
        title=title,
        description=description,
        amount=amount,
        buyer_email=buyer_email if buyer_email else None,
        status='ACTIVE',
        slug=slug,
        product_type=product_type,
    )

    db.session.add(new_link)
    try:
        db.session.commit()
    except Exception as e:
        db.session.rollback()
        return jsonify({"message": f"Database error: {str(e)}"}), 500

    return jsonify({
        "status": "success",
        "message": "Payment link created successfully",
        "data": new_link.to_dict()
    }), 201


@payment_links_bp.route('/vendor/payment-links', methods=['GET'])
@jwt_required()
def get_vendor_payment_links():
    user_id = get_jwt_identity()
    vendor = _get_vendor(user_id)
    if not vendor:
        return jsonify({"message": "Vendor access required"}), 403

    links = PaymentLink.query.filter_by(vendor_id=vendor.id).order_by(PaymentLink.created_at.desc()).all()
    
    # Calculate stats per link
    result = []
    for link in links:
        total_payouts = db.session.query(db.func.sum(Order.total_amount)).filter_by(
            payment_link_id=link.id, status='COMPLETED'
        ).scalar() or 0
        total_orders = Order.query.filter_by(payment_link_id=link.id).count()
        
        d = link.to_dict()
        d['total_revenue'] = str(total_payouts)
        d['total_orders'] = total_orders
        result.append(d)

    return jsonify(result), 200


@payment_links_bp.route('/vendor/payment-links/<int:link_id>', methods=['DELETE'])
@jwt_required()
def delete_payment_link(link_id):
    user_id = get_jwt_identity()
    vendor = _get_vendor(user_id)
    if not vendor:
        return jsonify({"message": "Vendor access required"}), 403

    link = PaymentLink.query.filter_by(id=link_id, vendor_id=vendor.id).first()
    if not link:
        return jsonify({"message": "Payment link not found"}), 404

    # Soft delete / deactivate
    link.status = 'EXPIRED'
    db.session.commit()
    return jsonify({"status": "success", "message": "Payment link deactivated"}), 200


# ===========================================================================
# PUBLIC ENDPOINTS
# ===========================================================================

@payment_links_bp.route('/marketplace/payment-links/<slug>', methods=['GET'])
def get_public_payment_link(slug):
    link = PaymentLink.query.filter_by(slug=slug).first()
    if not link:
        return jsonify({"message": "Payment link not found"}), 404

    sf = link.vendor.storefront if link.vendor else None
    
    return jsonify({
        "link": link.to_dict(),
        "vendor": {
            "store_name": sf.store_name if sf else (link.vendor.full_name if link.vendor else "Unknown Store"),
            "store_logo": sf.store_logo if sf else None,
            "store_slug": sf.store_slug if sf else None,
        }
    }), 200


@payment_links_bp.route('/marketplace/payment-links/<int:link_id>/pay', methods=['POST'])
def pay_payment_link(link_id):
    link = db.session.get(PaymentLink, link_id)
    if not link or link.status == 'EXPIRED':
        return jsonify({"message": "Payment link is inactive or expired."}), 400

    if link.link_type == 'INVOICE' and link.status == 'PAID':
        return jsonify({"message": "This invoice has already been paid."}), 400

    data = request.get_json() or {}
    buyer_name = (data.get('buyer_name') or '').strip()
    buyer_email = (data.get('buyer_email') or '').strip().lower()
    buyer_phone = (data.get('buyer_phone') or '').strip()

    if not buyer_name or not buyer_email or not buyer_phone:
        return jsonify({"message": "buyer_name, buyer_email, and buyer_phone are required"}), 400

    # ── PHYSICAL PRODUCT GUARD ────────────────────────────────────────────────
    # Pay Links for physical products must never route to Paystack.
    # payment_method must be 'bank_transfer' or 'crypto' for physical links.
    payment_method = (data.get('payment_method') or 'card').lower()
    link_product_type = getattr(link, 'product_type', 'service') or 'service'

    if link_product_type == 'physical' and payment_method == 'card':
        return jsonify({
            "message": (
                "Card payment is not available for physical products. "
                "Please use Bank Transfer or Crypto instead."
            ),
            "code": "PAYSTACK_PHYSICAL_BLOCKED",
        }), 400
    # ─────────────────────────────────────────────────────────────────────────

    # Calculate final amount: use link amount, or custom amount if open link
    if link.amount:
        amount = Decimal(str(link.amount))
    else:
        custom_amt = data.get('custom_amount')
        if custom_amt is None:
            return jsonify({"message": "Amount is required for this payment link"}), 400
        try:
            amount = Decimal(str(custom_amt))
            if amount <= 0:
                return jsonify({"message": "Amount must be greater than zero"}), 400
        except Exception:
            return jsonify({"message": "Invalid amount format"}), 400

    # Resolve buyer account: if exists, link to it; otherwise find/create a buyer user record
    from app.models.user import User
    buyer_user = User.query.filter_by(email=buyer_email).first()
    existing_account = buyer_user is not None  # track if this is a pre-existing Siiqo account

    if not buyer_user:
        # Create a light guest user — password unknown to buyer until they claim account
        buyer_user = User(
            email=buyer_email,
            phone=buyer_phone,
            role=UserRole.BUYER,
            is_verified=True,  # guest accounts bypass verification
        )
        buyer_user.set_password(uuid.uuid4().hex)
        parts = buyer_name.split(' ', 1)
        buyer_user.first_name = parts[0]
        if len(parts) > 1:
            buyer_user.last_name = parts[1]
        db.session.add(buyer_user)
        db.session.flush()

    # Create Direct Order
    new_order = Order(
        buyer_id=buyer_user.id,
        vendor_id=link.vendor_id,
        total_amount=amount,
        status='PENDING',
        payment_method='ESCROW',
        payment_link_id=link.id,
    )
    db.session.add(new_order)
    db.session.flush()

    # Add order item
    db.session.add(OrderItem(
        order_id=new_order.id,
        product_id=None,
        price_at_purchase=amount,
        quantity=1,
    ))

    # Calculate platform fees (6% fee)
    fee_percent = 6.00
    fee_amount = amount * Decimal('0.06')

    # Build the return URL so buyer lands on a proper success/tracking page
    site_url = os.environ.get('SITE_URL', 'https://siiqo.com').rstrip('/')
    import urllib.parse
    return_url = (
        f"{site_url}/pay/success"
        f"?order_id={new_order.id}"
        f"&email={urllib.parse.quote(buyer_email)}"
        f"&existing={str(existing_account).lower()}"
    )

    # Send a "claim your account" / OTP email to the guest buyer NOW (before payment)
    # so they have credentials ready when they return from the payment gateway.
    if not existing_account:
        import random
        from datetime import timedelta
        otp = str(random.randint(100000, 999999))
        buyer_user.reset_otp = otp
        buyer_user.otp_expiry = _utcnow() + timedelta(minutes=30)
        db.session.flush()
        try:
            from app.utils.email import send_siiqo_email
            send_siiqo_email(
                to_email=buyer_email,
                subject="Your Siiqo Order — Set a Password to Track It",
                template_name="guest_claim_account",
                first_name=buyer_user.first_name or "there",
                order_id=new_order.id,
                vendor_name=link.vendor.storefront.store_name if link.vendor and link.vendor.storefront else "Vendor",
                amount=f"₦{float(amount):,.2f}",
                claim_url=f"{site_url}/auth/reset-password-otp?email={urllib.parse.quote(buyer_email)}&otp={otp}&redirect=/user-profile&mode=claim",
                otp=otp,
            )
        except Exception as e:
            logging.warning(f"[PAYLINK] Guest claim-account email failed for {buyer_email}: {e}")

    # Initiate payment gateway transaction
    from app.services.escrow import get_escrow_provider

    if payment_method in ('bank_transfer', 'crypto'):
        # ── DAYA path ─────────────────────────────────────────────────────────
        daya_type = 'crypto_direct' if payment_method == 'crypto' else 'ngn_onramp'
        # Create order and escrow first, then initiate Daya funding account
        new_order.payment_method = 'CRYPTO'
        new_escrow = EscrowTransaction(
            order_id=new_order.id,
            transaction_number=f"ESC-{uuid.uuid4().hex[:12].upper()}",
            status=EscrowStatus.PENDING_PAYMENT,
            amount=float(amount),
            fee_percent=fee_percent,
            fee_amount=float(fee_amount),
        )
        db.session.add(new_escrow)
        db.session.commit()

        # Initiate Daya funding account
        try:
            from app.services import daya_service as _daya

            # 1. Get a firm exchange rate
            rate_data = _daya.get_rate(asset="USDT", side="BUY")
            rate = float(rate_data.get("rate", 1500))
            rate_id = rate_data.get("rate_id", "")
            rate_expires_at = rate_data.get("expires_at")

            # 2. Get or create Daya customer
            customer_id = _daya.get_or_create_customer(
                email=buyer_email,
                first_name=buyer_user.first_name or buyer_name.split()[0],
                last_name=buyer_user.last_name or (buyer_name.split()[1] if len(buyer_name.split()) > 1 else ''),
            )

            idempotency_key = f"paylink-{new_order.id}-{uuid.uuid4().hex[:8]}"

            # 3. Create the funding account based on type
            if daya_type == 'crypto_direct':
                fa = _daya.create_crypto_funding_account(
                    customer_id=str(customer_id),
                    asset="USDT",
                    network="TRC20",
                    rate_id=rate_id,
                    idempotency_key=idempotency_key,
                )
            else:
                # NGN onramp — amount must be integer kobo equivalent
                fa = _daya.create_ngn_funding_account(
                    customer_id=str(customer_id),
                    amount_ngn=int(float(amount)),
                    rate_id=rate_id,
                    idempotency_key=idempotency_key,
                )

            from app.models.withdrawal import DayaPayment
            from datetime import datetime as _dt_parse
            expires_dt = None
            if rate_expires_at:
                try:
                    expires_dt = _dt_parse.fromisoformat(str(rate_expires_at).replace('Z', '+00:00'))
                except Exception:
                    pass

            # Bank/wallet details are nested under fa["instructions"][0]
            instructions = fa.get("instructions", [{}])[0]

            dp = DayaPayment(
                order_id=new_order.id,
                buyer_id=buyer_user.id,
                payment_type=daya_type,
                daya_funding_account_id=str(fa.get('id') or ''),
                daya_rate_id=rate_id,
                amount_ngn=float(amount),
                rate=rate,
                rate_expires_at=expires_dt,
                status='PENDING',
                bank_name=instructions.get('bank_name'),
                account_number=instructions.get('account_number'),
                account_name=instructions.get('account_name'),
                wallet_address=instructions.get('address') or instructions.get('wallet_address'),
                network=instructions.get('network') or fa.get('chain') or fa.get('network'),
            )
            db.session.add(dp)
            db.session.commit()

            amount_usd = round(float(amount) / rate, 6)
            return jsonify({
                "success": True,
                "payment_method": payment_method,
                "order_id": new_order.id,
                "amount": str(amount),
                "daya": {
                    "bank_name": dp.bank_name,
                    "account_number": dp.account_number,
                    "account_name": dp.account_name,
                    "wallet_address": dp.wallet_address,
                    "network": dp.network,
                    "amount_ngn": float(amount),
                    "amount_usd": amount_usd,
                    "funding_type": daya_type,
                },
                "status": EscrowStatus.PENDING_PAYMENT,
            }), 200

        except Exception as e:
            logging.error(f"[PAYLINK DAYA] Failed to create Daya funding account: {e}")
            db.session.rollback()
            return jsonify({"message": f"Payment gateway error: {str(e)}"}), 503

    else:
        # ── PAYSTACK / CARD path (digital + service only) ──────────────────────
        provider = get_escrow_provider()
        result = provider.initiate_transaction([new_order], return_url=return_url)
        if not result.get("success"):
            db.session.rollback()
            return jsonify({"message": result.get("error_message") or "Payment gateway initialization failed"}), 400

        new_escrow = EscrowTransaction(
            order_id=new_order.id,
            transaction_number=result['transaction_number'],
            status=EscrowStatus.PENDING_PAYMENT,
            amount=float(amount),
            fee_percent=fee_percent,
            fee_amount=float(fee_amount),
            payment_link=result['payment_link'],
            payscrow_transaction_id=result['provider_transaction_id'],
            payscrow_ref=result['provider_reference'],
        )
        db.session.add(new_escrow)
        db.session.commit()

        return jsonify({
            "success": True,
            "paymentLink": result.get('payment_link'),
            "transactionNumber": result.get('transaction_number'),
            "amount": str(amount),
            "status": EscrowStatus.PENDING_PAYMENT,
            "order_id": new_order.id,
        }), 200
