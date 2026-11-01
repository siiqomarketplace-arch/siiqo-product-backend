"""
Phase 4 assistants: store concierge (8a) and seller copilot (8b).

Store concierge, public (buyer, no sign-in):
  GET  /api/concierge/<slug>                          is it on? store contact
  POST /api/concierge/<slug>/messages                 {session_id, message} -> reply
  GET  /api/concierge/<slug>/conversation?session_id= messages and offers (poll for approval)

Store concierge, seller (signed in):
  GET   /api/concierge/seller/settings                settings, usage this month, pending offers
  PATCH /api/concierge/seller/settings                {enabled?, auto_accept_within_floor?}
  GET   /api/concierge/seller/offers?status=          offers on the seller's store
  POST  /api/concierge/seller/offers/<id>/approve     code creates the Pay Link at the agreed price
  POST  /api/concierge/seller/offers/<id>/decline
  GET   /api/concierge/seller/conversations           recent buyer chats (text only)

Seller copilot (signed in):
  POST /api/copilot/ask                               {question, thread} -> answer + action buttons
  GET  /api/copilot/history?thread=                   this thread's questions and answers
  GET  /api/copilot/status                            questions used this month and the cap

The copilot has no endpoint that changes business data. Its action buttons
call existing endpoints (order status, chat, Pay Link form, offer approval).
"""
from flask import Blueprint, jsonify, request
from flask_jwt_extended import get_jwt_identity, jwt_required

from app.extensions import db, get_real_client_ip, limiter
from app.models.assistant import ConciergeConversation, ConciergeMessage, ConciergeOffer
from app.models.user import User, UserRole
from app.services import concierge as concierge_svc
from app.services import copilot as copilot_svc

concierge_bp = Blueprint("concierge", __name__)
copilot_bp = Blueprint("copilot", __name__)

OFFER_STATUSES = ("pending_seller", "declined", "link_created", "expired")


def _seller():
    """(user, storefront) for the signed-in seller, or (None, None)."""
    user = db.session.get(User, int(get_jwt_identity()))
    if user is None or user.storefront is None:
        return None, None
    if user.role not in (UserRole.VENDOR, UserRole.ADMIN) and user.storefront is None:
        return None, None
    return user, user.storefront


def _err(exc: concierge_svc.ConciergeError):
    body = {"code": exc.code, "message": exc.message}
    return jsonify(body), exc.status


# ── Concierge: seller ──────────────────────────────────────────────────────
@concierge_bp.route("/seller/settings", methods=["GET"])
@jwt_required()
def seller_settings():
    user, store = _seller()
    if store is None:
        return jsonify({"message": "Seller store required"}), 403
    return jsonify(concierge_svc.seller_summary(store)), 200


@concierge_bp.route("/seller/settings", methods=["PATCH"])
@jwt_required()
def update_seller_settings():
    user, store = _seller()
    if store is None:
        return jsonify({"message": "Seller store required"}), 403
    data = request.get_json(silent=True) or {}
    row = concierge_svc.settings_for(store, create=True)
    for key in ("enabled", "auto_accept_within_floor"):
        if key in data:
            if not isinstance(data[key], bool):
                db.session.rollback()
                return jsonify({"message": f"{key} must be true or false"}), 400
            setattr(row, key, data[key])
    db.session.commit()
    return jsonify(concierge_svc.seller_summary(store)), 200


@concierge_bp.route("/seller/offers", methods=["GET"])
@jwt_required()
def seller_offers():
    user, store = _seller()
    if store is None:
        return jsonify({"message": "Seller store required"}), 403
    q = ConciergeOffer.query.filter_by(storefront_id=store.id, vendor_id=user.id)
    status = (request.args.get("status") or "").strip()
    if status:
        if status not in OFFER_STATUSES:
            return jsonify({"message": "Unknown status"}), 400
        q = q.filter_by(status=status)
    rows = q.order_by(ConciergeOffer.id.desc()).limit(50).all()
    return jsonify({"offers": [o.to_dict(for_seller=True) for o in rows]}), 200


def _own_offer(offer_id: int):
    user, store = _seller()
    if store is None:
        return None, (jsonify({"message": "Seller store required"}), 403)
    offer = ConciergeOffer.query.filter_by(id=offer_id, storefront_id=store.id, vendor_id=user.id).first()
    if offer is None:
        return None, (jsonify({"message": "Offer not found"}), 404)
    return offer, None


@concierge_bp.route("/seller/offers/<int:offer_id>/approve", methods=["POST"])
@jwt_required()
def approve_offer(offer_id):
    offer, error = _own_offer(offer_id)
    if error:
        return error
    try:
        return jsonify({"offer": concierge_svc.approve_offer(offer)}), 200
    except concierge_svc.ConciergeError as exc:
        db.session.rollback()
        return _err(exc)


@concierge_bp.route("/seller/offers/<int:offer_id>/decline", methods=["POST"])
@jwt_required()
def decline_offer(offer_id):
    offer, error = _own_offer(offer_id)
    if error:
        return error
    try:
        return jsonify({"offer": concierge_svc.decline_offer(offer)}), 200
    except concierge_svc.ConciergeError as exc:
        return _err(exc)


@concierge_bp.route("/seller/conversations", methods=["GET"])
@jwt_required()
def seller_conversations():
    user, store = _seller()
    if store is None:
        return jsonify({"message": "Seller store required"}), 403
    convs = (ConciergeConversation.query.filter_by(storefront_id=store.id)
             .order_by(ConciergeConversation.updated_at.desc()).limit(20).all())
    out = []
    for c in convs:
        msgs = (ConciergeMessage.query.filter_by(conversation_id=c.id)
                .order_by(ConciergeMessage.id.asc()).limit(40).all())
        out.append({"id": c.id, "updated_at": c.updated_at.isoformat() if c.updated_at else None,
                    "messages": [m.to_dict() for m in msgs]})
    return jsonify({"conversations": out}), 200


# ── Concierge: public (buyer) ──────────────────────────────────────────────
@concierge_bp.route("/<string:slug>", methods=["GET"])
@limiter.limit("120 per minute")
def public_config(slug):
    store = concierge_svc.store_for_slug(slug)
    if store is None:
        return jsonify({"message": "Store not found"}), 404
    return jsonify(concierge_svc.public_config(store)), 200


@concierge_bp.route("/<string:slug>/messages", methods=["POST"])
@limiter.limit("30 per minute")
def post_message(slug):
    store = concierge_svc.store_for_slug(slug)
    if store is None:
        return jsonify({"message": "Store not found"}), 404
    data = request.get_json(silent=True) or {}
    try:
        body = concierge_svc.handle_message(store, str(data.get("session_id") or ""),
                                            str(data.get("message") or ""), get_real_client_ip())
    except concierge_svc.ConciergeError as exc:
        db.session.rollback()
        resp, status = _err(exc)
        if exc.code == "RATE_LIMITED":
            payload = resp.get_json()
            payload["contact"] = concierge_svc.contact_for(store)
            return jsonify(payload), status
        return resp, status
    return jsonify(body), 200


@concierge_bp.route("/<string:slug>/conversation", methods=["GET"])
@limiter.limit("60 per minute")
def get_conversation(slug):
    store = concierge_svc.store_for_slug(slug)
    if store is None or not concierge_svc.is_enabled(store):
        return jsonify({"message": "Not found"}), 404
    return jsonify(concierge_svc.history(store, request.args.get("session_id") or "")), 200


# ── Copilot ────────────────────────────────────────────────────────────────
@copilot_bp.route("/ask", methods=["POST"])
@jwt_required()
@limiter.limit("20 per minute")
def copilot_ask():
    user, store = _seller()
    if store is None:
        return jsonify({"message": "Seller store required"}), 403
    data = request.get_json(silent=True) or {}
    try:
        body = copilot_svc.ask(user, str(data.get("question") or ""), str(data.get("thread") or ""))
    except ValueError as exc:
        return jsonify({"message": str(exc)}), 400
    return jsonify(body), 200


@copilot_bp.route("/history", methods=["GET"])
@jwt_required()
def copilot_history():
    user, store = _seller()
    if store is None:
        return jsonify({"message": "Seller store required"}), 403
    return jsonify({"messages": copilot_svc.history(user, request.args.get("thread") or "")}), 200


@copilot_bp.route("/status", methods=["GET"])
@jwt_required()
def copilot_status():
    user, store = _seller()
    if store is None:
        return jsonify({"message": "Seller store required"}), 403
    return jsonify({"questions_this_month": copilot_svc.questions_this_month(user.id),
                    "monthly_cap": copilot_svc.monthly_cap(),
                    "max_tool_calls": copilot_svc.MAX_TOOL_CALLS}), 200
