import os
import uuid
import re
import logging
from datetime import datetime, timezone
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
    if not buyer_user:
        # Create a light guest user
        buyer_user = User(
            email=buyer_email,
            phone=buyer_phone,
            role=UserRole.BUYER,
            is_verified=True, # guest accounts bypass verification
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
    fee_percent = 12.00  # Siiqo standard transaction display fee
    fee_amount = amount * Decimal('0.12')

    # Initiate Payscrow Transaction
    from app.services.escrow import get_escrow_provider
    provider = get_escrow_provider()
    
    # We construct a list of orders for the provider API
    result = provider.initiate_transaction([new_order])
    if not result.get("success"):
        db.session.rollback()
        return jsonify({"message": result.get("error_message") or "Payment gateway initialization failed"}), 400

    # Save local EscrowTransaction details
    new_escrow = EscrowTransaction(
        order_id=new_order.id,
        transaction_number=result['transaction_number'],
        status=EscrowStatus.PENDING_PAYMENT,
        amount=float(amount),
        fee_percent=6.00,
        fee_amount=float(amount * Decimal('0.06')),  # Payscrow service split fee
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
