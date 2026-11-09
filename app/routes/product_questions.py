"""
Ask the seller a question without an account (BUYING_FIX_PLAN D5).

POST /api/public/products/<id>/question
    {name, email?, whatsapp?, message, website?}

The question reaches the seller as an in-app notification and an email. The
guest never sees the seller's email or phone; the seller replies to the email
or WhatsApp number the guest gave. ``website`` is a honeypot: bots that fill it
get a normal 201 and nothing is sent.
"""
import logging
import re

from flask import Blueprint, jsonify, request

from app.extensions import db, get_real_client_ip, limiter
from app.models.communication import Notification
from app.models.product import Product
from app.utils import email as email_utils

log = logging.getLogger(__name__)

product_questions_bp = Blueprint("product_questions", __name__)

MESSAGE_MIN, MESSAGE_MAX = 5, 1000
_EMAIL_RE = re.compile(r"^[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}$")


def _product_key():
    return f"product-question:{(request.view_args or {}).get('product_id')}"


def _clean_email(value):
    value = (value or "").strip().lower() if isinstance(value, str) else ""
    return value if len(value) <= 254 and _EMAIL_RE.match(value) else None


def _clean_phone(value):
    if not isinstance(value, (str, int)):
        return None
    raw = str(value).strip()
    digits = re.sub(r"\D", "", raw)
    if not 10 <= len(digits) <= 15:
        return None
    return ("+" if raw.startswith("+") else "") + digits


def _error(code, message, status=400):
    return jsonify({"code": code, "message": message}), status


@product_questions_bp.route("/products/<int:product_id>/question", methods=["POST"])
@limiter.limit("5 per hour", key_func=get_real_client_ip)
@limiter.limit("20 per day", key_func=_product_key)
def ask_seller(product_id):
    data = request.get_json(silent=True) or {}

    product = db.session.get(Product, product_id)
    sf = product.storefront if product is not None else None
    if (product is None or not product.is_active or product.is_deleted or sf is None
            or not sf.is_published or sf.vendor is None or not sf.vendor.is_active):
        return _error("PRODUCT_NOT_FOUND", "This product is no longer listed.", 404)

    if str(data.get("website") or "").strip():
        return jsonify({"status": "sent"}), 201   # honeypot: pretend, do nothing

    message = data.get("message")
    message = " ".join(message.split()) if isinstance(message, str) else ""
    if not MESSAGE_MIN <= len(message) <= MESSAGE_MAX:
        return _error("MESSAGE_REQUIRED",
                      f"Please write your question ({MESSAGE_MIN} to {MESSAGE_MAX} characters).")

    email = _clean_email(data.get("email"))
    whatsapp = _clean_phone(data.get("whatsapp") or data.get("phone"))
    if not email and not whatsapp:
        return _error("CONTACT_REQUIRED",
                      "Add your email or WhatsApp number so the seller can reply.")

    name = data.get("name")
    name = " ".join(name.split())[:80] if isinstance(name, str) and name.strip() else "A buyer"

    seller = sf.vendor
    contact_lines = [f"Email: {email}" if email else None, f"WhatsApp: {whatsapp}" if whatsapp else None]
    contact = ". ".join(c for c in contact_lines if c)
    note = (f"{name} asked about \"{product.name}\": {message}\n\n"
            f"Reply to them directly. {contact}.")

    db.session.add(Notification(
        user_id=seller.id,
        title=f"Question about {product.name}"[:255],
        message=note,
        type="BUYER_QUESTION",
    ))
    db.session.commit()

    try:
        from app.services.onboarding_publish import frontend_url
        email_utils.send_siiqo_email(
            to_email=seller.email,
            subject=f"A buyer asked about {product.name}"[:150],
            template_name="base_notification",
            first_name=seller.first_name or "there",
            title="You have a question from a buyer",
            message=note,
            cta_text="View the product",
            cta_url=f"{frontend_url()}/products/{product.id}",
        )
    except Exception as e:  # noqa: BLE001 - the in-app notification is already saved
        log.warning("product question email not sent for product %s: %s", product.id, e)

    try:
        from app.services.event_logger import log_platform_event
        log_platform_event(
            event_name="funnel.seller_contacted",
            session_id=(data.get("session_id") or request.headers.get("X-Session-ID")),
            business_id=seller.id,
            storefront_id=sf.id,
            product_id=product.id,
            source="server",
            properties={"channel": "guest_question"},
        )
    except Exception:
        pass

    return jsonify({"status": "sent"}), 201
