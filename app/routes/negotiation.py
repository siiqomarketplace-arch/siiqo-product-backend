import logging
"""
negotiation.py — Price negotiation API routes
"""
from datetime import datetime, timezone, timedelta

from flask import Blueprint, request, jsonify
from flask_jwt_extended import jwt_required, get_jwt_identity

from app.extensions import db
from app.models.negotiation import NegotiationRequest, NegotiationHistory
from app.models.order import Cart, CartItem
from app.models.product import Product
from app.models.user import User
from app.models.communication import Notification, Message
from app.utils.email import send_siiqo_email

negotiation_bp = Blueprint('negotiation', __name__)


def _utcnow():
    return datetime.now(timezone.utc)


def _notify(user_id: int, title: str, message: str, ntype: str = 'NEGOTIATION'):
    db.session.add(Notification(
        user_id=user_id,
        title=title,
        message=message,
        type=ntype,
    ))


def _chat_message(sender_id: int, receiver_id: int, content: str):
    """Send an automated chat message as part of the negotiation flow."""
    db.session.add(Message(
        sender_id=sender_id,
        receiver_id=receiver_id,
        content=content,
    ))


# ---------------------------------------------------------------------------
# POST /negotiations/create
# Buyer creates an offer on a negotiable product
# ---------------------------------------------------------------------------
@negotiation_bp.route('/create', methods=['POST'])
@jwt_required()
def create_offer():
    buyer_id = int(get_jwt_identity())
    data = request.get_json() or {}

    product_id    = data.get('product_id')
    cart_item_id  = data.get('cart_item_id')
    offered_price = data.get('offered_price')
    message       = (data.get('message') or '').strip()
    quantity      = int(data.get('quantity', 1))

    if not product_id or offered_price is None:
        return jsonify({"message": "product_id and offered_price are required"}), 400

    product = db.session.get(Product, int(product_id))
    if not product or not product.is_active:
        return jsonify({"message": "Product not found or inactive"}), 404

    if not product.is_negotiable:
        return jsonify({"message": "This product is not open for negotiation"}), 400

    offered_price = float(offered_price)
    original_price = float(product.price)

    if offered_price <= 0:
        return jsonify({"message": "Offer price must be greater than zero"}), 400

    # Floor price check (hidden from buyer — just reject silently with a generic message)
    if product.floor_price and offered_price < float(product.floor_price):
        return jsonify({"message": "Your offer is too low. Please try a higher amount."}), 400

    # Get vendor_id from storefront
    if not product.storefront:
        return jsonify({"message": "Product has no associated storefront"}), 400
    vendor_id = product.storefront.vendor_id

    if vendor_id == buyer_id:
        return jsonify({"message": "You cannot negotiate on your own product"}), 403

    # Check for an existing active negotiation on this product by this buyer
    existing = NegotiationRequest.query.filter(
        NegotiationRequest.buyer_id == buyer_id,
        NegotiationRequest.product_id == product_id,
        NegotiationRequest.status.in_(['PENDING', 'COUNTERED']),
    ).first()
    if existing:
        return jsonify({
            "message": "You already have an active negotiation for this product",
            "negotiation_id": existing.id,
        }), 409

    # Validate cart_item_id belongs to this buyer
    if cart_item_id:
        cart = Cart.query.filter_by(user_id=buyer_id).first()
        if not cart:
            cart_item_id = None
        else:
            ci = CartItem.query.filter_by(id=cart_item_id, cart_id=cart.id).first()
            if not ci:
                cart_item_id = None

    neg = NegotiationRequest(
        buyer_id=buyer_id,
        vendor_id=vendor_id,
        product_id=product_id,
        cart_item_id=cart_item_id,
        original_price=original_price,
        current_offer=offered_price,
        status='PENDING',
        awaiting_reply_from='vendor',
        buyer_message=message or None,
        quantity=quantity,
    )
    neg.set_expiry(48)
    db.session.add(neg)
    db.session.flush()

    # History entry
    db.session.add(NegotiationHistory(
        negotiation_id=neg.id,
        actor_id=buyer_id,
        action='PROPOSED',
        price=offered_price,
        message=message or None,
    ))

    # Notify vendor
    buyer = db.session.get(User, buyer_id)
    buyer_name = buyer.full_name if buyer else "A buyer"
    _notify(
        vendor_id,
        "New Price Offer",
        f"{buyer_name} offered ₦{offered_price:,.0f} for {product.name} (listed at ₦{original_price:,.0f}).",
        'NEGOTIATION',
    )

    # Auto chat message to vendor
    _chat_message(
        buyer_id, vendor_id,
        f"💬 Price Offer: I'd like to buy {product.name} for ₦{offered_price:,.0f} "
        f"(listed at ₦{original_price:,.0f}).{(' ' + message) if message else ''}",
    )

    db.session.commit()
    return jsonify({
        "message": "Offer submitted successfully",
        "negotiation": neg.to_dict(),
        "status": "success",
    }), 201


# ---------------------------------------------------------------------------
# GET /negotiations/buyer  — buyer's negotiations
# ---------------------------------------------------------------------------
@negotiation_bp.route('/buyer', methods=['GET'])
@jwt_required()
def get_buyer_negotiations():
    buyer_id = int(get_jwt_identity())
    status_filter = request.args.get('status')  # optional filter

    q = NegotiationRequest.query.filter_by(buyer_id=buyer_id)
    if status_filter:
        q = q.filter_by(status=status_filter.upper())
    negotiations = q.order_by(NegotiationRequest.updated_at.desc()).all()

    return jsonify({
        "negotiations": [n.to_dict() for n in negotiations],
        "total": len(negotiations),
    }), 200


# ---------------------------------------------------------------------------
# GET /negotiations/vendor  — vendor's negotiations
# ---------------------------------------------------------------------------
@negotiation_bp.route('/vendor', methods=['GET'])
@jwt_required()
def get_vendor_negotiations():
    vendor_id = int(get_jwt_identity())
    status_filter = request.args.get('status')

    q = NegotiationRequest.query.filter_by(vendor_id=vendor_id)
    if status_filter:
        q = q.filter_by(status=status_filter.upper())
    negotiations = q.order_by(NegotiationRequest.updated_at.desc()).all()

    # Counts per status
    counts = {}
    for s in ('PENDING', 'COUNTERED', 'ACCEPTED', 'REJECTED', 'EXPIRED'):
        counts[s.lower()] = NegotiationRequest.query.filter_by(
            vendor_id=vendor_id, status=s
        ).count()

    return jsonify({
        "negotiations": [n.to_dict() for n in negotiations],
        "total": len(negotiations),
        "counts": counts,
    }), 200


# ---------------------------------------------------------------------------
# GET /negotiations/<id>  — single negotiation detail
# ---------------------------------------------------------------------------
@negotiation_bp.route('/<int:neg_id>', methods=['GET'])
@jwt_required()
def get_negotiation(neg_id):
    user_id = int(get_jwt_identity())
    neg = db.session.get(NegotiationRequest, neg_id)
    if not neg:
        return jsonify({"message": "Negotiation not found"}), 404
    if neg.buyer_id != user_id and neg.vendor_id != user_id:
        return jsonify({"message": "Access denied"}), 403
    return jsonify({"negotiation": neg.to_dict()}), 200


# ---------------------------------------------------------------------------
# POST /negotiations/<id>/accept  — vendor accepts buyer's offer
# ---------------------------------------------------------------------------
@negotiation_bp.route('/<int:neg_id>/accept', methods=['POST'])
@jwt_required()
def accept_offer(neg_id):
    vendor_id = int(get_jwt_identity())
    neg = db.session.get(NegotiationRequest, neg_id)
    if not neg:
        return jsonify({"message": "Negotiation not found"}), 404
    if neg.vendor_id != vendor_id:
        return jsonify({"message": "Only the vendor can accept this offer"}), 403
    if neg.status not in ('PENDING', 'COUNTERED'):
        return jsonify({"message": f"Cannot accept a negotiation with status '{neg.status}'"}), 400
    if neg.is_expired():
        neg.status = 'EXPIRED'
        db.session.commit()
        return jsonify({"message": "This offer has expired"}), 410

    data = request.get_json() or {}
    vendor_message = (data.get('message') or '').strip()

    agreed_price = float(neg.current_offer)
    neg.status = 'ACCEPTED'
    neg.final_price = agreed_price
    neg.awaiting_reply_from = 'buyer'
    neg.vendor_message = vendor_message or None
    # Buyer has 48 h to checkout at this price
    neg.accepted_expires_at = _utcnow() + timedelta(hours=48)
    neg.expires_at = None  # no longer waiting for vendor response

    db.session.add(NegotiationHistory(
        negotiation_id=neg.id,
        actor_id=vendor_id,
        action='ACCEPTED',
        price=agreed_price,
        message=vendor_message or None,
    ))

    # Update cart item with negotiated price, or create it if missing
    cart = Cart.query.filter_by(user_id=neg.buyer_id).first()
    if not cart:
        cart = Cart(user_id=neg.buyer_id)
        db.session.add(cart)
        db.session.flush()

    ci = None
    if neg.cart_item_id:
        ci = db.session.get(CartItem, neg.cart_item_id)
        
    if not ci:
        ci = CartItem.query.filter_by(cart_id=cart.id, product_id=neg.product_id).first()
        
    if not ci:
        ci = CartItem(
            cart_id=cart.id,
            product_id=neg.product_id,
            quantity=neg.quantity or 1
        )
        db.session.add(ci)
        db.session.flush()

    ci.negotiated_price = agreed_price
    ci.negotiation_id = neg.id
    neg.cart_item_id = ci.id

    product = neg.product
    vendor = db.session.get(User, vendor_id)
    vendor_name = (vendor.storefront.store_name if vendor.storefront else vendor.full_name) if vendor else "The vendor"

    _notify(
        neg.buyer_id,
        "Offer Accepted! 🎉",
        f"{vendor_name} accepted your offer of ₦{agreed_price:,.0f} for {product.name if product else 'your item'}. "
        f"Complete your purchase within 48 hours.",
        'NEGOTIATION',
    )
    _chat_message(
        vendor_id, neg.buyer_id,
        f"✅ Offer Accepted! I've accepted your offer of ₦{agreed_price:,.0f} for "
        f"{product.name if product else 'the item'}. Please complete your purchase within 48 hours.",
    )

    buyer = db.session.get(User, neg.buyer_id)
    if buyer and buyer.email:
        try:
            send_siiqo_email(
                to_email=buyer.email,
                subject="Offer Accepted! 🎉",
                template_name="system_notice",
                first_name=buyer.first_name or "Buyer",
                title="Your Offer Was Accepted!",
                message=f"Good news! {vendor_name} has accepted your offer of ₦{agreed_price:,.0f} for {product.name if product else 'the item'}. Please proceed to your cart to complete the purchase within 48 hours."
            )
        except Exception as e:
            logging.warning(f"[EMAIL ERROR] Failed to send negotiation acceptance email: {e}")

    db.session.commit()
    return jsonify({
        "message": "Offer accepted",
        "negotiation": neg.to_dict(),
        "status": "success",
    }), 200


# ---------------------------------------------------------------------------
# POST /negotiations/<id>/counter  — vendor or buyer counters
# ---------------------------------------------------------------------------
@negotiation_bp.route('/<int:neg_id>/counter', methods=['POST'])
@jwt_required()
def counter_offer(neg_id):
    user_id = int(get_jwt_identity())
    neg = db.session.get(NegotiationRequest, neg_id)
    if not neg:
        return jsonify({"message": "Negotiation not found"}), 404

    # Determine role
    if user_id == neg.vendor_id:
        role = 'vendor'
        other_id = neg.buyer_id
    elif user_id == neg.buyer_id:
        role = 'buyer'
        other_id = neg.vendor_id
    else:
        return jsonify({"message": "Access denied"}), 403

    if neg.awaiting_reply_from != role:
        return jsonify({"message": f"It is not your turn to respond"}), 400
    if neg.status not in ('PENDING', 'COUNTERED'):
        return jsonify({"message": f"Cannot counter a negotiation with status '{neg.status}'"}), 400
    if neg.is_expired():
        neg.status = 'EXPIRED'
        db.session.commit()
        return jsonify({"message": "This negotiation has expired"}), 410

    data = request.get_json() or {}
    counter_price = data.get('counter_price')
    message = (data.get('message') or '').strip()

    if counter_price is None:
        return jsonify({"message": "counter_price is required"}), 400

    counter_price = float(counter_price)
    if counter_price <= 0:
        return jsonify({"message": "Counter price must be greater than zero"}), 400

    # Floor price check for vendor counters (shouldn't go below floor)
    product = neg.product
    if role == 'vendor' and product and product.floor_price:
        if counter_price < float(product.floor_price):
            return jsonify({"message": "Counter price cannot be below your floor price"}), 400

    neg.current_offer = counter_price
    neg.status = 'COUNTERED'
    neg.awaiting_reply_from = 'buyer' if role == 'vendor' else 'vendor'
    neg.set_expiry(48)

    if role == 'vendor':
        neg.vendor_message = message or None
    else:
        neg.buyer_message = message or None

    db.session.add(NegotiationHistory(
        negotiation_id=neg.id,
        actor_id=user_id,
        action='COUNTERED',
        price=counter_price,
        message=message or None,
    ))

    actor = db.session.get(User, user_id)
    actor_name = ((actor.storefront.store_name if actor.storefront else actor.full_name) if role == 'vendor' else actor.full_name) if actor else "The other party"
    product_name = product.name if product else "the item"

    _notify(
        other_id,
        "Counter-Offer Received",
        f"{actor_name} countered with ₦{counter_price:,.0f} for {product_name}.",
        'NEGOTIATION',
    )
    _chat_message(
        user_id, other_id,
        f"↩️ Counter-Offer: ₦{counter_price:,.0f} for {product_name}."
        f"{(' ' + message) if message else ''}",
    )

    db.session.commit()
    return jsonify({
        "message": "Counter-offer sent",
        "negotiation": neg.to_dict(),
        "status": "success",
    }), 200


# ---------------------------------------------------------------------------
# POST /negotiations/<id>/reject  — vendor or buyer rejects
# ---------------------------------------------------------------------------
@negotiation_bp.route('/<int:neg_id>/reject', methods=['POST'])
@jwt_required()
def reject_offer(neg_id):
    user_id = int(get_jwt_identity())
    neg = db.session.get(NegotiationRequest, neg_id)
    if not neg:
        return jsonify({"message": "Negotiation not found"}), 404

    if user_id not in (neg.vendor_id, neg.buyer_id):
        return jsonify({"message": "Access denied"}), 403
    if neg.status not in ('PENDING', 'COUNTERED'):
        return jsonify({"message": f"Cannot reject a negotiation with status '{neg.status}'"}), 400

    data = request.get_json() or {}
    message = (data.get('message') or '').strip()

    role = 'vendor' if user_id == neg.vendor_id else 'buyer'
    other_id = neg.buyer_id if role == 'vendor' else neg.vendor_id

    neg.status = 'REJECTED'
    neg.expires_at = None

    db.session.add(NegotiationHistory(
        negotiation_id=neg.id,
        actor_id=user_id,
        action='REJECTED',
        price=neg.current_offer,
        message=message or None,
    ))

    actor = db.session.get(User, user_id)
    actor_name = ((actor.storefront.store_name if actor.storefront else actor.full_name) if role == 'vendor' else actor.full_name) if actor else "The other party"
    product = neg.product
    product_name = product.name if product else "the item"

    _notify(
        other_id,
        "Offer Rejected",
        f"{actor_name} declined the offer for {product_name}. The listed price stands.",
        'NEGOTIATION',
    )
    _chat_message(
        user_id, other_id,
        f"❌ Offer Declined: The offer for {product_name} has been declined. "
        f"The listed price of ₦{float(neg.original_price):,.0f} stands."
        f"{(' ' + message) if message else ''}",
    )

    db.session.commit()
    return jsonify({
        "message": "Offer rejected",
        "negotiation": neg.to_dict(),
        "status": "success",
    }), 200


# ---------------------------------------------------------------------------
# POST /negotiations/<id>/buyer-accept  — buyer accepts a vendor counter-offer
# ---------------------------------------------------------------------------
@negotiation_bp.route('/<int:neg_id>/buyer-accept', methods=['POST'])
@jwt_required()
def buyer_accept_counter(neg_id):
    buyer_id = int(get_jwt_identity())
    neg = db.session.get(NegotiationRequest, neg_id)
    if not neg:
        return jsonify({"message": "Negotiation not found"}), 404
    if neg.buyer_id != buyer_id:
        return jsonify({"message": "Only the buyer can accept this counter-offer"}), 403
    if neg.status != 'COUNTERED':
        return jsonify({"message": "No counter-offer to accept"}), 400
    if neg.awaiting_reply_from != 'buyer':
        return jsonify({"message": "It is not your turn to respond"}), 400
    if neg.is_expired():
        neg.status = 'EXPIRED'
        db.session.commit()
        return jsonify({"message": "This offer has expired"}), 410

    agreed_price = float(neg.current_offer)
    neg.status = 'ACCEPTED'
    neg.final_price = agreed_price
    neg.awaiting_reply_from = 'none'
    neg.accepted_expires_at = _utcnow() + timedelta(hours=48)
    neg.expires_at = None

    db.session.add(NegotiationHistory(
        negotiation_id=neg.id,
        actor_id=buyer_id,
        action='ACCEPTED',
        price=agreed_price,
    ))

    # Update cart item with negotiated price, or create it if missing
    cart = Cart.query.filter_by(user_id=buyer_id).first()
    if not cart:
        cart = Cart(user_id=buyer_id)
        db.session.add(cart)
        db.session.flush()

    ci = None
    if neg.cart_item_id:
        ci = db.session.get(CartItem, neg.cart_item_id)
        
    if not ci:
        ci = CartItem.query.filter_by(cart_id=cart.id, product_id=neg.product_id).first()
        
    if not ci:
        ci = CartItem(
            cart_id=cart.id,
            product_id=neg.product_id,
            quantity=neg.quantity or 1
        )
        db.session.add(ci)
        db.session.flush()

    ci.negotiated_price = agreed_price
    ci.negotiation_id = neg.id
    neg.cart_item_id = ci.id

    product = neg.product
    buyer = db.session.get(User, buyer_id)
    buyer_name = buyer.full_name if buyer else "The buyer"

    _notify(
        neg.vendor_id,
        "Counter-Offer Accepted",
        f"{buyer_name} accepted your counter-offer of ₦{agreed_price:,.0f} for "
        f"{product.name if product else 'the item'}.",
        'NEGOTIATION',
    )
    _chat_message(
        buyer_id, neg.vendor_id,
        f"✅ I've accepted your counter-offer of ₦{agreed_price:,.0f} for "
        f"{product.name if product else 'the item'}. Proceeding to checkout.",
    )

    db.session.commit()
    return jsonify({
        "message": "Counter-offer accepted",
        "negotiation": neg.to_dict(),
        "status": "success",
    }), 200


# ---------------------------------------------------------------------------
# GET /negotiations/product/<product_id>  — check if buyer has active negotiation
# ---------------------------------------------------------------------------
@negotiation_bp.route('/product/<int:product_id>', methods=['GET'])
@jwt_required()
def get_product_negotiation(product_id):
    buyer_id = int(get_jwt_identity())
    neg = NegotiationRequest.query.filter(
        NegotiationRequest.buyer_id == buyer_id,
        NegotiationRequest.product_id == product_id,
        NegotiationRequest.status.in_(['PENDING', 'COUNTERED', 'ACCEPTED']),
    ).order_by(NegotiationRequest.updated_at.desc()).first()

    if not neg:
        return jsonify({"negotiation": None}), 200
    return jsonify({"negotiation": neg.to_dict()}), 200
