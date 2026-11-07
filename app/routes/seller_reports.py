"""
"Report this seller".

Public:  POST /api/check/report-seller            anyone (account optional)
Admin:   GET  /api/admin/seller-reports           any admin
         POST /api/admin/seller-reports/<id>      any admin: mark reviewed / dismissed
"""
import hashlib
import hmac
import logging
import re
from datetime import datetime, timedelta, timezone

from flask import Blueprint, current_app, jsonify, request
from flask_jwt_extended import get_jwt_identity, jwt_required

from app.extensions import db, get_real_client_ip, limiter
from app.models.seller_report import SellerReport
from app.models.user import Storefront

log = logging.getLogger(__name__)

seller_reports_bp = Blueprint('seller_reports', __name__)
seller_reports_admin_bp = Blueprint('seller_reports_admin', __name__)

_EMAIL_RE = re.compile(r"^[^@\s]{1,64}@[^@\s]{1,120}\.[a-z]{2,24}$", re.I)
MAX_PER_IP_PER_DAY = 5
REASON_LABELS = {
    "never_delivered": "Paid but never received the order",
    "fake_or_not_as_described": "Fake or not as described",
    "asked_to_pay_outside": "Asked me to pay outside Siiqo",
    "impersonation": "Pretending to be another business",
    "other": "Something else",
}


def _ip_hash(ip):
    key = (current_app.config.get("SECRET_KEY") or "siiqo").encode("utf-8")
    return hmac.new(key, f"seller-report:ip:{ip}".encode(), hashlib.sha256).hexdigest() if ip else None


def _user_id():
    try:
        identity = get_jwt_identity()
        return int(identity) if identity is not None else None
    except (TypeError, ValueError):
        return None


@seller_reports_bp.route('/report-seller', methods=['POST'])
@jwt_required(optional=True)
@limiter.limit("5 per hour")
def report_seller():
    body = request.get_json(silent=True) or {}
    slug = str(body.get("store_slug") or "").strip().lower()[:100]
    reason = str(body.get("reason") or "").strip()
    details = str(body.get("details") or "").strip()[:2000] or None
    order_ref = str(body.get("order_reference") or "").strip()[:60] or None
    email = str(body.get("email") or "").strip().lower()[:120] or None

    store = Storefront.query.filter_by(store_slug=slug).first() if slug else None
    if store is None:
        return jsonify({"code": "NOT_FOUND", "message": "We couldn't find that seller."}), 404
    if reason not in SellerReport.REASONS:
        return jsonify({"code": "REASON_REQUIRED", "message": "Choose what happened."}), 400
    if email and not _EMAIL_RE.match(email):
        return jsonify({"code": "INVALID_EMAIL", "message": "Check your email address, or leave it empty."}), 400

    ip_hash = _ip_hash(get_real_client_ip())
    since = datetime.now(timezone.utc) - timedelta(days=1)
    if ip_hash and SellerReport.query.filter(SellerReport.ip_hash == ip_hash,
                                             SellerReport.created_at >= since).count() >= MAX_PER_IP_PER_DAY:
        return jsonify({"code": "TOO_MANY", "message": "You've sent several reports today. Try again tomorrow."}), 429

    report = SellerReport(store_slug=slug, vendor_id=store.vendor_id, reason=reason, details=details,
                          order_reference=order_ref, contact_email=email, reporter_user_id=_user_id(),
                          ip_hash=ip_hash)
    db.session.add(report)
    db.session.commit()

    try:  # tell a person at Siiqo straight away; best effort
        from app.utils.email import send_siiqo_email
        support = current_app.config.get("MAIL_DEFAULT_SENDER") or "support@siiqo.com"
        send_siiqo_email(
            to_email=support,
            subject=f"Seller report #{report.id}: {store.store_name} ({REASON_LABELS[reason]})",
            template_name="base_notification",
            first_name="team",
            title="A buyer reported a seller",
            message=(f"Store: {store.store_name} ({slug}). Reason: {REASON_LABELS[reason]}. "
                     f"Order: {order_ref or 'not given'}. Details: {details or 'none'}. "
                     f"Reporter email: {email or 'not given'}."),
            cta_text="Open the admin",
            cta_url="https://siiqo.com/Adminstration",
        )
    except Exception as exc:  # noqa: BLE001
        log.info("seller report email not sent: %s", exc)

    return jsonify({"status": "success", "reference": f"RPT-{report.id}"}), 201


def _admin_or_none():
    from app.routes.admin import _get_admin, _parse_admin_id
    return _get_admin(_parse_admin_id(get_jwt_identity()))


def _public(r: SellerReport) -> dict:
    return {"id": r.id, "store_slug": r.store_slug, "reason": r.reason, "reason_label": REASON_LABELS.get(r.reason),
            "details": r.details, "order_reference": r.order_reference, "contact_email": r.contact_email,
            "status": r.status, "created_at": r.created_at.isoformat() if r.created_at else None}


@seller_reports_admin_bp.route('', methods=['GET'])
@jwt_required()
def list_reports():
    if not _admin_or_none():
        return jsonify({"message": "Unauthorized"}), 403
    status = request.args.get("status") or "open"
    q = SellerReport.query
    if status in SellerReport.STATUSES:
        q = q.filter(SellerReport.status == status)
    rows = q.order_by(SellerReport.created_at.desc()).limit(200).all()
    return jsonify({"reports": [_public(r) for r in rows]}), 200


@seller_reports_admin_bp.route('/<int:report_id>', methods=['POST'])
@jwt_required()
def update_report(report_id):
    if not _admin_or_none():
        return jsonify({"message": "Unauthorized"}), 403
    report = db.session.get(SellerReport, report_id)
    if report is None:
        return jsonify({"message": "Not found"}), 404
    status = str((request.get_json(silent=True) or {}).get("status") or "")
    if status not in SellerReport.STATUSES:
        return jsonify({"message": "Unknown status"}), 400
    report.status = status
    db.session.commit()
    return jsonify({"report": _public(report)}), 200
