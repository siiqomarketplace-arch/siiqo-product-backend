"""
Privacy and data-protection endpoints (mounted at /api/privacy).

Mechanisms for the data-subject rights in the Nigeria Data Protection Act 2023:
  GET  /export                         download your own data (JSON)
  GET  /consents                       your current consent choices
  POST /consents                       record a consent choice
  GET  /requests                       your privacy requests
  POST /requests                       open a privacy request
  GET  /admin/requests                 (superadmin) list requests
  POST /admin/requests/<id>/resolve    (superadmin) close a request

Account deletion is not duplicated here: it stays at DELETE /api/auth/delete-account,
which anonymises the account (app/services/accounts.py).
"""
import json
import logging
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

from flask import Blueprint, Response, jsonify, request
from flask_jwt_extended import get_jwt_identity, jwt_required

from app.extensions import db, limiter
from app.models.privacy import (
    CONSENT_DESCRIPTIONS,
    CONSENT_VERSIONS,
    Consent,
    ConsentPurpose,
    PrivacyRequest,
    PrivacyRequestStatus,
    PrivacyRequestType,
)
from app.models.user import User

privacy_bp = Blueprint('privacy', __name__)

EXPORTS_PER_DAY = 3
MAX_OPEN_REQUESTS = 5
MAX_DETAILS_CHARS = 2000

DELETE_ACCOUNT_FLOW = {
    "method": "DELETE",
    "path": "/api/auth/delete-account",
    "body": {"password": "<your password>"},
    "note": ("Deleting your account removes your personal details and closes your store. "
             "Orders, payments and reviews are kept without your details so other people's "
             "records stay correct. Open orders must be finished or cancelled first."),
}
EXPORT_FLOW = {"method": "GET", "path": "/api/privacy/export"}


def _utcnow():
    return datetime.now(timezone.utc)


def _iso(value):
    if value is None:
        return None
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, Decimal):
        return float(value)
    return value


def _current_user():
    """The signed-in person, or None for admin tokens and unknown ids."""
    try:
        user_id = int(get_jwt_identity())
    except (TypeError, ValueError):
        return None
    return db.session.get(User, user_id)


def _admin():
    from app.routes.admin import _parse_admin_id, _require_superadmin
    return _require_superadmin(_parse_admin_id(get_jwt_identity()))


# ---------------------------------------------------------------------------
# Consents
# ---------------------------------------------------------------------------

def _latest_consents(user_id):
    """Latest row per purpose (rows are append-only, so the highest id wins)."""
    latest = {}
    for row in Consent.query.filter_by(user_id=user_id).order_by(Consent.id.asc()).all():
        latest[row.purpose] = row
    return latest


def current_consents(user):
    """Current choice for every purpose. Marketing email follows the account's
    broadcast flag, which the one-click unsubscribe link and account deletion
    also change, so that flag is the source of truth."""
    latest = _latest_consents(user.id)
    out = {}
    for purpose in ConsentPurpose.ALL:
        row = latest.get(purpose)
        item = row.to_dict() if row else {
            "purpose": purpose, "version": None, "granted": False,
            "source": "default", "created_at": None,
        }
        if purpose == ConsentPurpose.MARKETING_EMAIL:
            flag = bool(user.is_subscribed_to_broadcasts)
            if row is None or bool(row.granted) != flag:
                item = {"purpose": purpose, "version": None, "granted": flag,
                        "source": "account_setting", "created_at": None}
        item["description"] = CONSENT_DESCRIPTIONS[purpose]
        item["current_version"] = CONSENT_VERSIONS[purpose]
        out[purpose] = item
    return out


def record_consent(user, purpose, granted, source="user"):
    """Append a consent choice (callers commit)."""
    row = Consent(user_id=user.id, purpose=purpose, version=CONSENT_VERSIONS[purpose],
                  granted=bool(granted), source=source)
    db.session.add(row)
    if purpose == ConsentPurpose.MARKETING_EMAIL:
        user.is_subscribed_to_broadcasts = bool(granted)
    return row


@privacy_bp.route('/consents', methods=['GET'])
@jwt_required()
def get_consents():
    user = _current_user()
    if not user or not user.is_active:
        return jsonify({"message": "Unauthorized"}), 403
    return jsonify({"consents": current_consents(user)}), 200


@privacy_bp.route('/consents', methods=['POST'])
@jwt_required()
@limiter.limit("60 per hour")
def set_consent():
    user = _current_user()
    if not user or not user.is_active:
        return jsonify({"message": "Unauthorized"}), 403
    data = request.get_json(silent=True) or {}
    purpose = (data.get('purpose') or '').strip()
    granted = data.get('granted')
    if purpose not in ConsentPurpose.ALL:
        return jsonify({"message": "Unknown purpose", "purposes": list(ConsentPurpose.ALL)}), 400
    if not isinstance(granted, bool):
        return jsonify({"message": "granted must be true or false"}), 400

    row = record_consent(user, purpose, granted)
    db.session.commit()
    return jsonify({"consent": row.to_dict(), "consents": current_consents(user)}), 201


# ---------------------------------------------------------------------------
# Privacy requests
# ---------------------------------------------------------------------------

@privacy_bp.route('/requests', methods=['GET'])
@jwt_required()
def list_my_requests():
    user = _current_user()
    if not user:
        return jsonify({"message": "Unauthorized"}), 403
    rows = (PrivacyRequest.query.filter_by(user_id=user.id)
            .order_by(PrivacyRequest.created_at.desc(), PrivacyRequest.id.desc()).all())
    return jsonify({"requests": [r.to_dict() for r in rows]}), 200


@privacy_bp.route('/requests', methods=['POST'])
@jwt_required()
@limiter.limit("10 per hour")
def create_request():
    user = _current_user()
    if not user or not user.is_active:
        return jsonify({"message": "Unauthorized"}), 403
    data = request.get_json(silent=True) or {}
    req_type = (data.get('type') or '').strip().lower()
    details = data.get('details')
    if req_type not in PrivacyRequestType.ALL:
        return jsonify({"message": "Unknown request type", "types": list(PrivacyRequestType.ALL)}), 400
    if details is not None and not isinstance(details, str):
        return jsonify({"message": "details must be text"}), 400
    details = (details or '').strip()
    if len(details) > MAX_DETAILS_CHARS:
        return jsonify({"message": f"details must be at most {MAX_DETAILS_CHARS} characters"}), 400

    # Deletion already has a self-service flow; don't open a parallel one.
    if req_type == PrivacyRequestType.DELETION:
        return jsonify({
            "created": False,
            "code": "USE_SELF_SERVICE_DELETE",
            "message": "You can delete your account yourself from your account settings.",
            "self_service": DELETE_ACCOUNT_FLOW,
        }), 200

    if req_type in (PrivacyRequestType.CORRECTION, PrivacyRequestType.OBJECTION,
                    PrivacyRequestType.RESTRICTION) and not details:
        return jsonify({"message": "Please tell us what this request is about."}), 400

    open_count = PrivacyRequest.query.filter_by(
        user_id=user.id, status=PrivacyRequestStatus.OPEN, channel="request").count()
    if open_count >= MAX_OPEN_REQUESTS:
        return jsonify({"message": "You already have several open requests. "
                                   "We'll answer those first."}), 429

    now = _utcnow()
    row = PrivacyRequest(user_id=user.id, type=req_type, details=details or None,
                         status=PrivacyRequestStatus.OPEN, channel="request",
                         created_at=now, due_at=PrivacyRequest.default_due(now))
    db.session.add(row)
    db.session.commit()

    body = {"created": True, "request": row.to_dict()}
    if req_type in (PrivacyRequestType.ACCESS, PrivacyRequestType.PORTABILITY):
        body["self_service"] = EXPORT_FLOW
    return jsonify(body), 201


@privacy_bp.route('/admin/requests', methods=['GET'])
@jwt_required()
def admin_list_requests():
    if not _admin():
        return jsonify({"message": "Unauthorized"}), 403
    q = PrivacyRequest.query.filter_by(channel="request")
    status = (request.args.get('status') or '').strip().lower()
    if status:
        if status not in PrivacyRequestStatus.ALL:
            return jsonify({"message": "Unknown status"}), 400
        q = q.filter_by(status=status)
    req_type = (request.args.get('type') or '').strip().lower()
    if req_type:
        if req_type not in PrivacyRequestType.ALL:
            return jsonify({"message": "Unknown request type"}), 400
        q = q.filter_by(type=req_type)
    try:
        limit = max(1, min(int(request.args.get('limit', 100)), 500))
    except (TypeError, ValueError):
        limit = 100
    rows = q.order_by(PrivacyRequest.created_at.asc(), PrivacyRequest.id.asc()).limit(limit).all()

    users = {u.id: u for u in User.query.filter(User.id.in_({r.user_id for r in rows})).all()} if rows else {}
    out = []
    for r in rows:
        item = r.to_dict(admin=True)
        u = users.get(r.user_id)
        item["user"] = {"id": r.user_id, "email": u.email if u else None,
                        "name": u.full_name if u else None}
        out.append(item)
    return jsonify({"requests": out}), 200


@privacy_bp.route('/admin/requests/<int:request_id>/resolve', methods=['POST'])
@jwt_required()
def admin_resolve_request(request_id):
    admin = _admin()
    if not admin:
        return jsonify({"message": "Unauthorized"}), 403
    row = db.session.get(PrivacyRequest, request_id)
    if not row or row.channel != "request":
        return jsonify({"message": "Request not found"}), 404
    if row.status != PrivacyRequestStatus.OPEN:
        return jsonify({"message": "Request is already closed"}), 409

    data = request.get_json(silent=True) or {}
    status = (data.get('status') or '').strip().lower()
    note = data.get('note')
    if status not in (PrivacyRequestStatus.DONE, PrivacyRequestStatus.REJECTED):
        return jsonify({"message": "status must be 'done' or 'rejected'"}), 400
    if note is not None and not isinstance(note, str):
        return jsonify({"message": "note must be text"}), 400
    note = (note or '').strip()
    if status == PrivacyRequestStatus.REJECTED and not note:
        return jsonify({"message": "Give the person a reason when rejecting a request."}), 400
    if len(note) > MAX_DETAILS_CHARS:
        return jsonify({"message": f"note must be at most {MAX_DETAILS_CHARS} characters"}), 400

    row.status = status
    row.resolution_note = note or None
    row.resolved_at = _utcnow()
    row.resolved_by_admin_id = admin.id
    db.session.commit()

    try:
        from app.models.audit import AdminAuditLog
        AdminAuditLog.log_action(admin, 'PRIVACY_REQUEST_RESOLVE', 'PrivacyRequest', row.id,
                                 details={"status": status, "type": row.type, "user_id": row.user_id})
    except Exception:
        db.session.rollback()
        logging.exception("[privacy] audit log failed for request %s", row.id)

    return jsonify({"request": row.to_dict(admin=True)}), 200


# ---------------------------------------------------------------------------
# Data export
# ---------------------------------------------------------------------------

def _mask(value, keep=4):
    if not value:
        return None
    value = str(value)
    return ("*" * max(len(value) - keep, 0)) + value[-keep:]


def _account(user):
    return {
        "id": user.id,
        "email": user.email,
        "email_verified": bool(user.is_verified),
        "first_name": user.first_name,
        "last_name": user.last_name,
        "phone": user.phone,
        "profile_pic": user.profile_pic,
        "role": user.role,
        "is_active": bool(user.is_active),
        "is_subscribed_to_broadcasts": bool(user.is_subscribed_to_broadcasts),
        "referral_code": user.referral_code,
        "points_balance": _iso(user.points_balance),
        "telegram_id": user.telegram_id,
        "telegram_notification_prefs": user.telegram_notification_prefs or {},
        "preferred_locale": user.preferred_locale,
        "city": user.city,
        "state": user.state,
        "created_at": _iso(user.created_at),
        "updated_at": _iso(user.updated_at),
    }


# Storefront columns that are internal or are documents/IDs we don't hand out.
_STORE_EXCLUDE = {"paystack_subaccount_code", "nin_document_url", "cac_document_url",
                  "onboarding_emails_sent", "account_number"}


def _store(sf):
    if sf is None:
        return None
    data = {c.name: _iso(getattr(sf, c.name)) for c in sf.__table__.columns
            if c.name not in _STORE_EXCLUDE}
    data["account_number_masked"] = _mask(sf.account_number)
    return data


def _products(sf):
    if sf is None:
        return []
    from app.models.product import Product
    rows = Product.query.filter_by(storefront_id=sf.id).order_by(Product.id.asc()).all()
    return [{c.name: _iso(getattr(p, c.name)) for c in p.__table__.columns} for p in rows]


def _order_core(o, escrow_status):
    return {
        "id": o.id,
        "status": o.status,
        "payment_method": o.payment_method,
        "currency": o.currency,
        "total_amount": _iso(o.total_amount),
        "logistics_fee": _iso(o.logistics_fee),
        "tracking_number": o.tracking_number,
        "escrow_status": escrow_status,
        "created_at": _iso(o.created_at),
        "paid_at": _iso(o.paid_at),
        "delivered_at": _iso(o.delivered_at),
        "items": [{
            "product_id": it.product_id,
            "product_name": it.product.name if it.product else None,
            "price_at_purchase": _iso(it.price_at_purchase),
            "quantity": it.quantity,
        } for it in o.items],
    }


def _orders(user):
    from app.models.escrow import EscrowTransaction
    from app.models.order import Order
    from app.models.user import Storefront

    as_buyer = Order.query.filter_by(buyer_id=user.id).order_by(Order.id.asc()).all()
    as_seller = Order.query.filter_by(vendor_id=user.id).order_by(Order.id.asc()).all()
    ids = [o.id for o in as_buyer + as_seller]
    escrow = {e.order_id: e.status for e in
              EscrowTransaction.query.filter(EscrowTransaction.order_id.in_(ids)).all()} if ids else {}
    stores = {s.vendor_id: s.store_name for s in Storefront.query.filter(
        Storefront.vendor_id.in_({o.vendor_id for o in as_buyer})).all()} if as_buyer else {}

    buyer_rows = []
    for o in as_buyer:
        item = _order_core(o, escrow.get(o.id))
        item["seller_store_name"] = stores.get(o.vendor_id)
        # The buyer's own details as given at checkout.
        item["your_details"] = {
            "name": o.buyer_name, "email": o.buyer_email, "phone": o.buyer_phone,
            "delivery_name": o.delivery_name, "delivery_phone": o.delivery_phone,
            "delivery_address": o.delivery_address, "delivery_city": o.delivery_city,
            "delivery_state": o.delivery_state,
        }
        buyer_rows.append(item)

    seller_rows = []
    for o in as_seller:
        item = _order_core(o, escrow.get(o.id))
        # Data minimisation: the buyer's name and delivery area only; their email,
        # phone and street address are the buyer's data, not part of this export.
        item["buyer"] = {"name": o.buyer_name, "delivery_city": o.delivery_city,
                         "delivery_state": o.delivery_state}
        seller_rows.append(item)
    return buyer_rows, seller_rows


def _reviews(user):
    from app.models.community import Review
    from app.models.user import Storefront

    written = Review.query.filter_by(buyer_id=user.id).order_by(Review.id.asc()).all()
    received = Review.query.filter_by(vendor_id=user.id).order_by(Review.id.asc()).all()
    stores = {s.vendor_id: s.store_name for s in Storefront.query.filter(
        Storefront.vendor_id.in_({r.vendor_id for r in written})).all()} if written else {}

    def base(r):
        return {"id": r.id, "order_id": r.order_id, "product_id": r.product_id,
                "vendor_rating": r.vendor_rating, "product_rating": r.product_rating,
                "review_text": r.review_text, "is_approved": bool(r.is_approved),
                "created_at": _iso(r.created_at)}

    out_written = []
    for r in written:
        item = base(r)
        item["seller_store_name"] = stores.get(r.vendor_id)
        out_written.append(item)
    return out_written, [base(r) for r in received]


def _billing(user):
    from app.models.admin import VendorSubscription
    from app.models.billing import SubscriptionPayment

    subs = VendorSubscription.query.filter_by(vendor_id=user.id).order_by(VendorSubscription.id.asc()).all()
    pays = SubscriptionPayment.query.filter_by(vendor_id=user.id).order_by(SubscriptionPayment.id.asc()).all()
    return (
        [{"id": s.id, "plan": s.plan.name if s.plan else None, "status": s.status,
          "start_date": _iso(s.start_date), "end_date": _iso(s.end_date)} for s in subs],
        [{"id": p.id, "subscription_id": p.subscription_id, "reference": p.reference,
          "amount_ngn": _iso(p.amount_ngn), "status": p.status, "refund_reason": p.refund_reason,
          "refunded_at": _iso(p.refunded_at), "created_at": _iso(p.created_at)} for p in pays],
    )


def _verification(user):
    """Outcome only: never the NIN/BVN/RC number, its hash, provider references or documents."""
    from app.models.verification import VerificationCase, VerificationStatus

    st = VerificationStatus.query.filter_by(user_id=user.id).first()
    cases = VerificationCase.query.filter_by(user_id=user.id).order_by(VerificationCase.id.asc()).all()
    return {
        "status": {
            "seller_type": st.seller_type,
            "identity_verified_at": _iso(st.identity_verified_at),
            "business_verified_at": _iso(st.business_verified_at),
            "valid_until": _iso(st.valid_until),
            "subscription_active": bool(st.subscription_active),
        } if st else None,
        "cases": [{
            "id": c.id,
            "kind": c.kind,
            "seller_type": c.seller_type,
            "status": c.status,
            "reason_code": c.reason_code,
            "verified_name": c.verified_name,
            "business_name": c.business_name,
            "consent_version": c.consent_version,
            "consent_at": _iso(c.consent_at),
            "created_at": _iso(c.created_at),
            "completed_at": _iso(c.completed_at),
            "checks": [{"check_type": ch.check_type, "result": ch.result,
                        "created_at": _iso(ch.created_at)} for ch in c.checks],
        } for c in cases],
        **_verification_lifecycle(user),
    }


def _verification_lifecycle(user):
    """Re-check state, expiry reminders and lifecycle events (2-3): the seller's
    own rows only. Admin identity and internal admin notes are never included."""
    from app.models.verification import (VerificationAuditEvent, VerificationReminder,
                                         VerificationWatch)
    from app.services.verification.rechecks import reason_text

    watch = VerificationWatch.query.filter_by(user_id=user.id).first()
    reminders = (VerificationReminder.query.filter_by(user_id=user.id)
                 .order_by(VerificationReminder.id.asc()).all())
    events = (VerificationAuditEvent.query.filter_by(user_id=user.id)
              .order_by(VerificationAuditEvent.id.asc()).all())
    safe_detail_keys = ("kind", "valid_until", "grace_until", "disputes", "window_days", "case_id")
    return {
        "recheck": {
            "baseline_store_name": watch.baseline_store_name,
            "baseline_payout_name": watch.baseline_payout_name,
            "baseline_at": _iso(watch.baseline_at),
            "reason_code": watch.recheck_reason_code,
            "reason": reason_text(watch.recheck_reason_code),
            "requested_at": _iso(watch.recheck_requested_at),
            "reverify_by": _iso(watch.recheck_grace_until),
            "paused_at": _iso(watch.recheck_paused_at),
        } if watch else None,
        "reminders": [{"kind": r.kind, "for_check_valid_until": r.cycle, "sent_at": _iso(r.sent_at)}
                      for r in reminders],
        "events": [{
            "event": e.event,
            "reason_code": e.reason_code,
            "by": "Siiqo" if e.actor_type == "ADMIN" else e.actor_type.lower(),
            "details": {k: v for k, v in (e.details or {}).items() if k in safe_detail_keys} or None,
            "created_at": _iso(e.created_at),
        } for e in events],
    }


def _address_verification(user):
    """Address checks (V5): outcome and dates only. The address and NIN were sent
    to the provider and never stored by Siiqo, so they cannot appear here."""
    from app.services.address_verification.service import export_for
    return export_for(user)


def _passport(user):
    from app.models.passport import PassportSettings
    ps = PassportSettings.query.filter_by(vendor_id=user.id).first()
    if not ps:
        return None
    return {"is_public": bool(ps.is_public), "show_completed_orders": bool(ps.show_completed_orders),
            "show_member_since": bool(ps.show_member_since), "claimed_at": _iso(ps.claimed_at),
            "updated_at": _iso(ps.updated_at)}


def _notifications(user):
    from app.models.communication import Notification
    rows = Notification.query.filter_by(user_id=user.id).order_by(Notification.id.asc()).all()
    return [{"id": n.id, "title": n.title, "message": n.message, "type": n.type,
             "is_read": bool(n.is_read), "order_id": n.order_id,
             "created_at": _iso(n.created_at)} for n in rows]


def build_export(user):
    sf = user.storefront
    orders_as_buyer, orders_as_seller = _orders(user)
    reviews_written, reviews_received = _reviews(user)
    subscriptions, subscription_payments = _billing(user)
    return {
        "export": {
            "format_version": 1,
            "generated_at": _utcnow().isoformat(),
            "notes": [
                "This file holds the personal data Siiqo keeps about your account.",
                "Not included: your password (only a one-way hash is kept), one-time codes, "
                "identity numbers (NIN/BVN/RC are never stored, only a one-way check value), "
                "and other people's contact details.",
                "Chat messages are not included; you can read them in the app.",
            ],
        },
        "account": _account(user),
        "store": _store(sf),
        "products": _products(sf),
        "orders_as_buyer": orders_as_buyer,
        "orders_as_seller": orders_as_seller,
        "reviews_written": reviews_written,
        "reviews_received": reviews_received,
        "subscriptions": subscriptions,
        "subscription_payments": subscription_payments,
        "verification": _verification(user),
        "address_verification": _address_verification(user),
        "passport_settings": _passport(user),
        "consents": {
            "current": current_consents(user),
            "history": [c.to_dict() for c in
                        Consent.query.filter_by(user_id=user.id).order_by(Consent.id.asc()).all()],
        },
        "privacy_requests": [r.to_dict() for r in
                             PrivacyRequest.query.filter_by(user_id=user.id)
                             .order_by(PrivacyRequest.id.asc()).all()],
        "notifications": _notifications(user),
    }


@privacy_bp.route('/export', methods=['GET'])
@jwt_required()
@limiter.limit("10 per hour")
def export_my_data():
    user = _current_user()
    if not user or not user.is_active:
        return jsonify({"message": "Unauthorized"}), 403

    # Per-person daily cap, counted in the database so it holds across workers.
    since = _utcnow() - timedelta(days=1)
    recent = PrivacyRequest.query.filter(
        PrivacyRequest.user_id == user.id,
        PrivacyRequest.type == PrivacyRequestType.PORTABILITY,
        PrivacyRequest.channel == "self_service",
        PrivacyRequest.created_at >= since,
    ).count()
    if recent >= EXPORTS_PER_DAY:
        return jsonify({"message": f"You can download your data {EXPORTS_PER_DAY} times a day. "
                                   "Please try again tomorrow.", "code": "EXPORT_LIMIT"}), 429

    try:
        payload = build_export(user)
    except Exception:
        db.session.rollback()
        logging.exception("[privacy] export failed for user %s", user.id)
        return jsonify({"message": "We couldn't build your export. Please try again."}), 500

    now = _utcnow()
    db.session.add(PrivacyRequest(
        user_id=user.id, type=PrivacyRequestType.PORTABILITY, details="Self-service data export",
        status=PrivacyRequestStatus.DONE, channel="self_service", created_at=now, resolved_at=now))
    db.session.commit()

    body = json.dumps(payload, indent=2, ensure_ascii=False, default=_iso)
    filename = f"siiqo-data-export-{user.id}-{now.strftime('%Y%m%d')}.json"
    return Response(body, status=200, mimetype='application/json', headers={
        "Content-Disposition": f'attachment; filename="{filename}"',
        "Cache-Control": "no-store",
    })
