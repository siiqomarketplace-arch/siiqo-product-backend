# cart.py - Cart and checkout routes
import logging
import uuid
from datetime import datetime, timezone

from flask import Blueprint, request, jsonify
from flask_jwt_extended import jwt_required, get_jwt_identity

from app.extensions import db
from app.models.order import Cart, CartItem, Order, OrderItem
from app.models.product import Product
from app.models.user import User
from app.models.escrow import EscrowTransaction, EscrowStatus
from app.models.finance import Ledger, Invoice
from app.models.crm import CustomerProfile
from app.models.partnerships import Referral
from app.models.communication import Notification
from app.models.withdrawal import PODPayment, VendorBankAccount
from app.utils.email import send_siiqo_email
from app.utils.telegram import send_telegram_message

cart_bp = Blueprint('cart', __name__)


def _utcnow():
    return datetime.now(timezone.utc)


# ---------------------------------------------------------------------------
# Helper: vendor crypto wallet fields for cart items (Daya integration)
# ---------------------------------------------------------------------------

def _vendor_crypto_fields(vendor_id) -> dict:
    """
    Return Daya crypto payment fields for a vendor.
    One indexed DB lookup per vendor. Safe to call with vendor_id=None.
    """
    if not vendor_id:
        return {
            "vendor_accepts_crypto": False,
            "vendor_crypto_asset":   "USDT",
            "vendor_crypto_network": "TRC20",
        }
    try:
        from app.models.withdrawal import VendorCryptoWallet
        wallet = VendorCryptoWallet.query.filter_by(
            vendor_id=vendor_id, accepts_crypto=True
        ).first()
        return {
            "vendor_accepts_crypto": bool(wallet),
            "vendor_crypto_asset":   wallet.asset   if wallet else "USDT",
            "vendor_crypto_network": wallet.network if wallet else "TRC20",
        }
    except Exception:
        return {
            "vendor_accepts_crypto": False,
            "vendor_crypto_asset":   "USDT",
            "vendor_crypto_network": "TRC20",
        }


# ---------------------------------------------------------------------------
# GET /cart/
# ---------------------------------------------------------------------------

@cart_bp.route('/', methods=['GET'])
@jwt_required()
def get_cart():
    user_id = get_jwt_identity()
    cart = Cart.query.filter_by(user_id=user_id).first()

    if not cart:
        return jsonify({"items": [], "total": "0", "item_count": 0}), 200

    items = []
    total = 0
    for item in cart.items:
        if item.product and item.product.is_active:
            subtotal = float(item.product.price) * item.quantity
            total += subtotal
            sf = item.product.storefront
            items.append({
                "id": item.id,
                "product_id": item.product.id,
                "name": item.product.name,
                "price": str(item.product.price),
                "unit_price": float(item.product.price),
                "quantity": item.quantity,
                "subtotal": str(subtotal),
                "image": item.product.images[0] if item.product.images else None,
                "storefront": sf.store_name if sf else None,
                "storefront_slug": sf.store_slug if sf else None,
                "vendor_name": sf.store_name if sf else None,
                "vendor_id": sf.vendor_id if sf else None,
                "vendor_has_bank": bool(
                    (sf.vendor_id and db.session.query(db.exists().where(VendorBankAccount.vendor_id == sf.vendor_id)).scalar()) or
                    (sf and sf.bank_code and sf.account_number)
                ) if sf else False,
                # Daya crypto payment fields
                **_vendor_crypto_fields(sf.vendor_id if sf else None),
                "stock_quantity": item.product.stock_quantity,
                "category": item.product.category.name if item.product.category else "",
                "product_type": (item.product.product_type if item.product else 'physical') or 'physical',
                "file_url": item.product.file_url if item.product else None,
                "booking_link": item.product.booking_link if item.product else None,
                "is_negotiable": item.product.is_negotiable,
                "negotiated_price": str(item.negotiated_price) if item.negotiated_price else None,
                "negotiation_id": item.negotiation_id,
                "negotiation_status": item.negotiation.status if item.negotiation else None,
                "negotiation_current_offer": str(item.negotiation.current_offer) if item.negotiation else None,
            })

    return jsonify({
        "cart_id": cart.id,
        "items": items,
        "total": str(total),
        "item_count": len(items),
    }), 200


# ---------------------------------------------------------------------------
# POST /cart/add
# ---------------------------------------------------------------------------

@cart_bp.route('/add', methods=['POST'])
@jwt_required()
def add_to_cart():
    user_id = get_jwt_identity()
    data = request.get_json() or {}
    product_id = data.get('product_id')
    quantity = int(data.get('quantity', 1))

    if not product_id:
        return jsonify({"message": "product_id is required"}), 400

    product = db.session.get(Product, product_id)
    if not product or not product.is_active:
        return jsonify({"message": "Product is not available"}), 400

    if product.storefront.vendor_id == int(user_id):
        return jsonify({"message": "You cannot purchase your own product."}), 403

    if product.stock_quantity < quantity:
        return jsonify({"message": f"Only {product.stock_quantity} in stock"}), 400

    cart = Cart.query.filter_by(user_id=user_id).first()
    if not cart:
        cart = Cart(user_id=user_id)
        db.session.add(cart)
        db.session.flush()

    existing = CartItem.query.filter_by(cart_id=cart.id, product_id=product_id).first()
    if existing:
        if existing.quantity + quantity > product.stock_quantity:
            return jsonify({"message": f"Cannot add more. Only {product.stock_quantity} total available in stock."}), 400
        existing.quantity += quantity
    else:
        db.session.add(CartItem(cart_id=cart.id, product_id=product_id, quantity=quantity))

    try:
        db.session.commit()
    except Exception as e:
        db.session.rollback()
        return jsonify({"message": f"Database error: {str(e)}"}), 500
    return jsonify({"message": "Item added to cart", "status": "success"}), 200


# ---------------------------------------------------------------------------
# PATCH /cart/update/<item_id>
# ---------------------------------------------------------------------------

@cart_bp.route('/update/<int:item_id>', methods=['PATCH'])
@jwt_required()
def update_cart_item(item_id):
    user_id = get_jwt_identity()
    data = request.get_json() or {}
    quantity = data.get('quantity')

    if quantity is None or int(quantity) < 1:
        return jsonify({"message": "Invalid quantity"}), 400

    cart = Cart.query.filter_by(user_id=user_id).first()
    if not cart:
        return jsonify({"message": "Cart not found"}), 404

    item = CartItem.query.filter_by(id=item_id, cart_id=cart.id).first()
    if not item:
        return jsonify({"message": "Item not found"}), 404

    product = db.session.get(Product, item.product_id)
    if product and product.stock_quantity < int(quantity):
        return jsonify({"message": f"Cannot update. Only {product.stock_quantity} available in stock."}), 400

    item.quantity = int(quantity)
    try:
        db.session.commit()
    except Exception as e:
        db.session.rollback()
        return jsonify({"message": f"Database error: {str(e)}"}), 500
    return jsonify({"message": "Cart updated", "status": "success"}), 200


# ---------------------------------------------------------------------------
# DELETE /cart/remove/<item_id>
# ---------------------------------------------------------------------------

@cart_bp.route('/remove/<int:item_id>', methods=['DELETE'])
@jwt_required()
def remove_cart_item(item_id):
    user_id = get_jwt_identity()
    cart = Cart.query.filter_by(user_id=user_id).first()
    if not cart:
        return jsonify({"message": "Cart not found"}), 404

    item = CartItem.query.filter_by(id=item_id, cart_id=cart.id).first()
    if not item:
        return jsonify({"message": "Item not found"}), 404

    from app.models.negotiation import NegotiationRequest
    NegotiationRequest.query.filter_by(cart_item_id=item.id).update({"cart_item_id": None}, synchronize_session=False)

    db.session.delete(item)
    try:
        db.session.commit()
    except Exception as e:
        db.session.rollback()
        return jsonify({"message": f"Database error: {str(e)}"}), 500
    return jsonify({"message": "Item removed", "status": "success"}), 200


# ---------------------------------------------------------------------------
# DELETE /cart/clear
# ---------------------------------------------------------------------------

@cart_bp.route('/clear', methods=['DELETE'])
@jwt_required()
def clear_cart():
    user_id = get_jwt_identity()
    cart = Cart.query.filter_by(user_id=user_id).first()
    if cart:
        items = CartItem.query.filter_by(cart_id=cart.id).all()
        item_ids = [item.id for item in items]
        if item_ids:
            from app.models.negotiation import NegotiationRequest
            NegotiationRequest.query.filter(NegotiationRequest.cart_item_id.in_(item_ids)).update({"cart_item_id": None}, synchronize_session=False)
            CartItem.query.filter_by(cart_id=cart.id).delete(synchronize_session=False)
            try:
                db.session.commit()
            except Exception as e:
                db.session.rollback()
                return jsonify({"message": f"Database error: {str(e)}"}), 500
    return jsonify({"message": "Cart cleared", "status": "success"}), 200


# ---------------------------------------------------------------------------
# POST /cart/checkout - the canonical checkout
# ---------------------------------------------------------------------------

@cart_bp.route('/checkout', methods=['POST'])
@jwt_required(optional=True)
def checkout():
    user_id = get_jwt_identity()
    user = db.session.get(User, int(user_id)) if user_id else None
    cart = Cart.query.filter_by(user_id=user_id).first() if user_id else None

    data = request.get_json() or {}
    guest_items_input = data.get('items', [])

    # Simple wrapper object if checking out from guest cart items array
    class _GuestItem:
        def __init__(self, item_id, product, quantity, negotiated_price=None, negotiation=None):
            self.id = item_id
            self.product = product
            self.quantity = quantity
            self.negotiated_price = negotiated_price
            self.negotiation = negotiation

    items_to_process = []
    if cart and cart.items and not guest_items_input:
        items_to_process = list(cart.items)
    elif guest_items_input:
        for idx, gi in enumerate(guest_items_input):
            p_id = gi.get('product_id') or (gi.get('product', {}).get('id'))
            if not p_id:
                continue
            prod = db.session.get(Product, int(p_id))
            if prod:
                qty = int(gi.get('quantity', 1))
                neg_price = gi.get('negotiated_price')
                items_to_process.append(_GuestItem(idx, prod, qty, neg_price))
    
    if not items_to_process:
        return jsonify({"message": "Your cart is empty"}), 400

    referral_code = (data.get('referral_code') or '').strip().upper()
    delivery_address = data.get('delivery_address', '')

    # Get payment method (ESCROW, POD, or CRYPTO)
    payment_method = (data.get('payment_method') or 'ESCROW').upper()
    if payment_method not in ['ESCROW', 'POD', 'CRYPTO']:
        payment_method = 'ESCROW'

    filter_vendor_name = (data.get('vendor_name') or '').strip().lower()
    filter_vendor_id = None
    try:
        raw_vid = data.get('vendor_id')
        filter_vendor_id = int(raw_vid) if raw_vid else None
    except (ValueError, TypeError):
        filter_vendor_id = None

    vendors: dict[tuple[int, bool], list] = {}
    skipped_items = []
    for item in items_to_process:
        if not (item.product and item.product.is_active and item.product.storefront):
            continue
        sf = item.product.storefront
        vid = sf.vendor_id
        if filter_vendor_id and vid != filter_vendor_id:
            continue
        if filter_vendor_name and sf.store_name.lower() != filter_vendor_name:
            continue
        if item.negotiation and item.negotiation.status in ('PENDING', 'COUNTERED'):
            skipped_items.append({
                "product_name": item.product.name,
                "reason": "Offer pending -- checkout at agreed price once vendor responds",
            })
            continue
        if item.negotiation and item.negotiation.status == 'ACCEPTED':
            from app.models.negotiation import NegotiationRequest
            neg = item.negotiation
            if neg.accepted_expires_at:
                from datetime import timezone as _tz
                exp = neg.accepted_expires_at
                if exp.tzinfo is None:
                    exp = exp.replace(tzinfo=_tz.utc)
                if datetime.now(_tz.utc) > exp:
                    item.negotiated_price = None
                    item.negotiation_id = None
                    neg.status = 'EXPIRED'
        p_type = (item.product.product_type if item.product else 'physical') or 'physical'
        is_physical = (p_type == 'physical')
        vendors.setdefault((vid, is_physical), []).append(item)

    if not vendors:
        return jsonify({"message": "No valid items in cart"}), 400

    # Validate POD access (exclusive to Pro Subscribed vendors)
    if payment_method == 'POD':
        from app.models.admin import VendorSubscription
        now_dt = _utcnow()
        for (vid, is_physical) in vendors.keys():
            active_sub = VendorSubscription.query.filter_by(
                vendor_id=vid, status='ACTIVE'
            ).filter(VendorSubscription.end_date > now_dt).first()
            if not active_sub:
                vendor_user = db.session.get(User, vid)
                v_name = (vendor_user.storefront.store_name if (vendor_user and vendor_user.storefront) else "This seller")
                return jsonify({
                    "message": f"Pay on Delivery is an exclusive feature for Pro Subscribed vendors. '{v_name}' does not currently have active POD permissions. Please pay securely via Escrow or Crypto.",
                    "code": "POD_PRO_REQUIRED"
                }), 400

    # Validate bank accounts for ESCROW (not needed for CRYPTO or POD)
    if payment_method == 'ESCROW':
        for (vid, is_physical) in vendors.keys():
            bank_acc = VendorBankAccount.query.filter_by(vendor_id=vid, is_default=True).first()
            if not bank_acc:
                bank_acc = VendorBankAccount.query.filter_by(vendor_id=vid).first()
            if not bank_acc:
                vendor_user = db.session.get(User, vid)
                sf = vendor_user.storefront if vendor_user else None
                if not (sf and sf.bank_code and sf.account_number):
                    v_name = sf.store_name if sf else (vendor_user.full_name if vendor_user else f"Vendor ID {vid}")
                    return jsonify({"message": f"Escrow payment is unavailable because the vendor '{v_name}' has not configured their payout bank details. Please contact the vendor to update their details or choose a different payment method."}), 400

    orders_created = []

    buyer_order_count = Order.query.filter_by(buyer_id=user_id).count()
    if buyer_order_count < 100:
        from app.models.partnerships import Referral
        existing_ref = Referral.query.filter_by(referred_id=user_id).first()
        if not existing_ref and referral_code:
            referrer = User.query.filter_by(referral_code=referral_code).first()
            if referrer and referrer.id != int(user_id):
                existing_ref = Referral(
                    referrer_id=referrer.id,
                    referred_id=user_id,
                    referral_code_used=referral_code,
                    status='PENDING',
                    reward_earned=0.0,
                )
                db.session.add(existing_ref)
                db.session.flush()

    logistics_selections = data.get('logistics_selections', [])

    for (vid, is_physical), items in vendors.items():
        total = sum(
            float(item.negotiated_price if item.negotiated_price else item.product.price) * item.quantity
            for item in items
        )
        vendor_user = db.session.get(User, vid)
        is_pro = bool(vendor_user and vendor_user.storefront and vendor_user.storefront.is_pro_active)
        fee_percent = 3.00 if is_pro else 5.00
        fee_amount = total * (fee_percent / 100)
        has_physical_items = is_physical
        logistics_fee = 0.0
        logistics_provider_id = None

        if has_physical_items and logistics_selections:
            for sel in logistics_selections:
                sel_vid = sel.get('vendorId')
                if sel_vid is not None and int(sel_vid) == int(vid):
                    opt = sel.get('selectedOption') or {}
                    logistics_fee = float(opt.get('fee', 0.0))
                    logistics_provider_id = opt.get('id')
                    break

        guest_email = (data.get('delivery_email') or data.get('email') or (data.get('customer', {}).get('email')) or (user.email if user else '')).strip().lower()
        guest_name = (data.get('delivery_name') or (data.get('customer', {}).get('name')) or (user.full_name if user else '')).strip()

        new_order = Order(
            buyer_id=int(user_id) if user_id else None,
            vendor_id=vid,
            total_amount=total,
            status='PENDING',
            payment_method=payment_method,
            buyer_email=guest_email,
            buyer_name=guest_name,
            is_guest=(user_id is None),
            logistics_provider_id=logistics_provider_id,
            logistics_fee=logistics_fee,
            delivery_address=data.get('delivery_address') if has_physical_items else 'Digital Delivery',
            delivery_city=data.get('delivery_city') if has_physical_items else None,
            delivery_state=data.get('delivery_state') if has_physical_items else None,
            delivery_phone=data.get('delivery_phone') if has_physical_items else None,
            delivery_name=guest_name or (data.get('customer', {}).get('name') if data.get('customer') else None),
        )
        db.session.add(new_order)
        db.session.flush()

        if logistics_provider_id and logistics_provider_id.startswith('siiqo_partner_'):
            pid_str = logistics_provider_id.replace('siiqo_partner_', '')
            if pid_str.isdigit():
                partner_id = int(pid_str)
                partner_base_fee = round(logistics_fee / 1.10, 2)
                from app.models.escrow import LogisticsAssignment
                assignment = LogisticsAssignment(
                    order_id=new_order.id,
                    partner_id=partner_id,
                    status='ASSIGNED' if payment_method == 'POD' else 'PENDING',
                    delivery_fee=partner_base_fee,
                    assigned_at=datetime.utcnow() if payment_method == 'POD' else None
                )
                db.session.add(assignment)

        for item in items:
            product = db.session.query(Product).with_for_update().get(item.product.id)
            p_type = (product.product_type or 'physical')
            if p_type == 'physical':
                if product.stock_quantity < item.quantity:
                    db.session.rollback()
                    return jsonify({"message": f"Insufficient stock for {product.name}"}), 400
                product.stock_quantity -= item.quantity

            effective_price = item.negotiated_price if item.negotiated_price else product.price
            db.session.add(OrderItem(
                order_id=new_order.id,
                product_id=product.id,
                price_at_purchase=effective_price,
                quantity=item.quantity,
            ))

        if payment_method == 'POD':
            pod_payment = PODPayment(
                order_id=new_order.id,
                vendor_id=vid,
                amount=total,
                currency='NGN',
                confirmed_by_vendor=False,
            )
            db.session.add(pod_payment)
            new_order.status = 'PENDING_DELIVERY'
            db.session.add(Notification(
                user_id=vid,
                title="New POD Order Received",
                message=f"You have a new Pay on Delivery order #{new_order.id} worth NGN{total:,.2f}. Deliver and collect payment.",
                type="ORDER",
                order_id=new_order.id,
            ))
            vendor = db.session.get(User, vid)
            if vendor:
                try:
                    send_siiqo_email(
                        to_email=vendor.email,
                        subject="New Pay-on-Delivery Order - Siiqo",
                        template_name="order_received_vendor",
                        first_name=vendor.first_name or "Vendor",
                        order_id=new_order.id,
                        total_amount=f"NGN{total:,.2f}",
                        payment_method="POD"
                    )
                except Exception:
                    pass
            orders_created.append({
                "order_id": new_order.id,
                "vendor_id": vid,
                "total_amount": str(total),
                "payment_method": "POD",
                "status": "PENDING_DELIVERY",
            })

        elif payment_method == 'CRYPTO':
            # For crypto, create an escrow transaction record in PENDING_PAYMENT state.
            # The DayaPayment record is created by /payments/daya/initiate AFTER checkout.
            # When Daya confirms payment, _handle_crypto_payment_confirmed() updates both.
            fee_rate = 0.03 if (new_order.vendor and new_order.vendor.storefront and new_order.vendor.storefront.is_pro_active) else 0.05
            txn_number = f"ESC-{uuid.uuid4().hex[:12].upper()}"
            new_escrow = EscrowTransaction(
                order_id=new_order.id,
                transaction_number=txn_number,
                status=EscrowStatus.PENDING_PAYMENT,
                amount=total,
                fee_percent=fee_rate * 100,
                fee_amount=round(total * fee_rate, 2),
                currency='NGN',
            )
            db.session.add(new_escrow)
            orders_created.append({
                "order_id": new_order.id,
                "id": new_order.id,
                "vendor_id": vid,
                "total_amount": str(total),
                "escrow_txn": txn_number,
                "payment_method": "CRYPTO",
            })

        else:  # ESCROW (default)
            txn_number = f"ESC-{uuid.uuid4().hex[:12].upper()}"
            new_escrow = EscrowTransaction(
                order_id=new_order.id,
                transaction_number=txn_number,
                status=EscrowStatus.PENDING_PAYMENT,
                amount=total,
                fee_percent=fee_percent,
                fee_amount=fee_amount,
                currency='NGN',
            )
            db.session.add(new_escrow)
            orders_created.append({
                "order_id": new_order.id,
                "vendor_id": vid,
                "total_amount": str(total),
                "escrow_txn": txn_number,
                "payment_method": "ESCROW",
            })

        db.session.add(Invoice(
            order_id=new_order.id,
            vendor_id=vid,
            buyer_id=user_id,
        ))

        # Push direct Telegram alert to vendor
        vendor_obj = db.session.get(User, vid)
        if vendor_obj and vendor_obj.telegram_id:
            try:
                # Check if this is the vendor's first order
                prev_orders = Order.query.filter(
                    Order.vendor_id == vid,
                    Order.id != new_order.id
                ).count()
                first_product_name = items[0].product.name if items and items[0].product else "an item"
                vendor_fname = vendor_obj.first_name or "Seller"

                if prev_orders == 0:
                    tg_text = (
                        f"🎉 <b>CONGRATULATIONS {vendor_fname.upper()}! YOUR FIRST SALE ON SIIQO!</b>\n\n"
                        f"A customer just ordered <b>{first_product_name}</b> (Order #{new_order.id}) "
                        f"worth <b>NGN{total:,.2f}</b> ({payment_method})!\n\n"
                        f"You are officially an active Siiqo Seller. Login to view and fulfill your order:\n"
                        f"👉 <a href='https://siiqo.com/vendor/orders'>View & Fulfill Order</a>"
                    )
                else:
                    tg_text = (
                        f"🛒 <b>New Order Received!</b>\n\n"
                        f"A customer just ordered <b>{first_product_name}</b> (Order #{new_order.id}) "
                        f"worth <b>NGN{total:,.2f}</b> ({payment_method}).\n\n"
                        f"👉 <a href='https://siiqo.com/vendor/orders'>View & Fulfill Order</a>"
                    )
                send_telegram_message(vendor_obj.telegram_id, tg_text)
            except Exception as tg_err:
                logging.warning(f"[TELEGRAM PUSH WARN] Failed to notify vendor {vid}: {tg_err}")

        if payment_method == 'POD':
            try:
                send_siiqo_email(
                    to_email=user.email,
                    subject=f"Order Confirmation #{new_order.id} - Siiqo",
                    template_name="order_confirmation",
                    first_name=user.first_name or "there",
                    order_id=new_order.id,
                    payment_method=payment_method,
                )
            except Exception as e:
                logging.warning(f"[EMAIL WARN] Failed to send order confirmation to buyer: {e}")

        profile = CustomerProfile.query.filter_by(vendor_id=vid, buyer_id=user_id).first()
        if profile:
            profile.total_spent = float(profile.total_spent or 0) + total
            profile.total_orders = (profile.total_orders or 0) + 1
            profile.last_purchase_date = _utcnow()
            if profile.total_orders >= 10:
                profile.segment = 'VIP'
            elif profile.total_orders >= 3:
                profile.segment = 'REGULAR'
        else:
            db.session.add(CustomerProfile(
                vendor_id=vid,
                buyer_id=user_id,
                total_spent=total,
                total_orders=1,
                segment='NEW',
                last_purchase_date=_utcnow(),
            ))

    checked_out_item_ids = [
        item.id
        for items_list in vendors.values()
        for item in items_list
    ]

    if checked_out_item_ids:
        from app.models.negotiation import NegotiationRequest
        NegotiationRequest.query.filter(
            NegotiationRequest.cart_item_id.in_(checked_out_item_ids)
        ).update({"cart_item_id": None}, synchronize_session=False)

    if cart and checked_out_item_ids:
        CartItem.query.filter(
            CartItem.cart_id == cart.id,
            CartItem.id.in_(checked_out_item_ids)
        ).delete(synchronize_session=False)

    try:
        db.session.commit()
    except Exception as e:
        db.session.rollback()
        return jsonify({"message": f"Checkout failed: {str(e)}"}), 500

    return jsonify({
        "message": "Checkout successful. Proceed to payment.",
        "orders": orders_created,
        "id": orders_created[0]["order_id"] if orders_created else None,
        "status": "success",
        "skipped_items": skipped_items,
    }), 200
