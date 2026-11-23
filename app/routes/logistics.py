import logging
"""
logistics.py — Logistics partner routes
"""
import os
import secrets
import string
from datetime import datetime, timezone

from flask import Blueprint, g, request, jsonify
from flask_jwt_extended import jwt_required, get_jwt_identity

from app.extensions import db
from app.models.user import Storefront, User, UserRole
from app.models.escrow import LogisticsAssignment
from app.models.partnerships import PartnerApplication, PartnerStaff
from app.models.communication import Notification
from app.services.delivery import (DeliveryError, clean_delivery_settings, public_delivery_settings,
                                   vendor_self_fee)
from app.services.logistics_flow import FlowError, actor_for, assignment_public, move
from app.services.logistics_partners import (approved_logistics_partners, partner_access, partner_coverage,
                                             partner_price_for, partner_required, quote_options)

logistics_bp = Blueprint('logistics', __name__)


def _utcnow():
    return datetime.now(timezone.utc)


# ---------------------------------------------------------------------------
# GET/POST /logistics/settings  — vendor logistics preferences
# ---------------------------------------------------------------------------

@logistics_bp.route('/settings', methods=['GET', 'POST'])
@jwt_required(optional=True)
def vendor_logistics_settings():
    # Allow buyer-side fetch with ?vendor_id=X (no auth needed)
    vendor_id_param = request.args.get('vendor_id')
    if vendor_id_param and request.method == 'GET':
        try:
            from app.models.user import Storefront
            sf = Storefront.query.filter_by(vendor_id=int(vendor_id_param)).first()
            if sf:
                return jsonify({
                    "enabled_logistics": sf.logistics_settings or [],
                    "store_city": sf.city,
                    "store_state": sf.state,
                    "delivery_settings": public_delivery_settings(sf),
                }), 200
        except Exception:
            pass
        return jsonify({"enabled_logistics": [], "store_city": None, "store_state": None,
                        "delivery_settings": None}), 200

    user_id = get_jwt_identity()
    if not user_id:
        return jsonify({"message": "Authentication required"}), 401
    user = db.session.get(User, int(user_id))
    if not user or not user.storefront:
        return jsonify({"message": "Vendor storefront required"}), 403

    sf = user.storefront

    if request.method == 'GET':
        return jsonify({
            "enabled_logistics": sf.logistics_settings or [],
            "store_city": sf.city,
            "store_state": sf.state,
            "delivery_settings": public_delivery_settings(sf),
        }), 200

    data = request.get_json() or {}
    if 'delivery_settings' in data:
        try:
            sf.delivery_settings = clean_delivery_settings(data.get('delivery_settings'))
        except DeliveryError as e:
            return jsonify({"code": e.code, "message": e.message}), e.status
    if 'enabled_logistics' in data or 'delivery_settings' not in data:
        chosen = data.get('enabled_logistics', [])
        if not isinstance(chosen, list):
            return jsonify({"message": "enabled_logistics must be a list"}), 400
        # Option ids only (e.g. "self_pickup", "siiqo_partner_12"); checkout
        # offers only the partners listed here when any are listed.
        sf.logistics_settings = [str(x)[:60] for x in chosen if isinstance(x, (str, int))][:50]
    if 'store_city' in data:
        sf.city = data['store_city']
    if 'store_state' in data:
        sf.state = data['store_state']
    db.session.commit()
    return jsonify({"message": "Logistics settings updated", "status": "success"}), 200


# ---------------------------------------------------------------------------
# GET /logistics/active  — public list of approved logistics partners
# (sellers pick from this list; checkout uses /logistics/quote)
# ---------------------------------------------------------------------------

@logistics_bp.route('/active', methods=['GET'])
def get_active_partners():
    role_filter = (request.args.get('role') or 'LOGISTICS').upper()
    # Financing partners are no longer shown to anyone (Siiqo doesn't give
    # or arrange loans). Their records are kept; they are just not listed.
    from app.services.removed_content import REMOVED_PARTNER_TYPES
    if role_filter in REMOVED_PARTNER_TYPES or role_filter != 'LOGISTICS':
        return jsonify({"status": "success", "data": []}), 200
    state_filter = (request.args.get('state') or '').strip()

    partner_data = []
    for p, app_row in approved_logistics_partners():
        if state_filter and partner_price_for(app_row, state_filter) is None:
            continue
        coverage = partner_coverage(app_row)
        pricing = app_row.pricing_settings if isinstance(app_row.pricing_settings, dict) else {}
        # Public endpoint: never expose partner email, bank details or API keys.
        partner_data.append({
            "id": p.id,
            "name": p.full_name,
            # Shown to sellers when choosing delivery partners for a listing.
            "business_name": app_row.business_name or p.full_name,
            "service_type": "LOGISTICS",
            "partner_role": "LOGISTICS",
            "state": ", ".join(c["state"] for c in coverage),
            "status": "ACTIVE",
            "coverage": coverage,
            "store_settings": {
                "pricing_model": pricing.get("pricing_model") or "FLAT",
                "flat_rate": pricing.get("flat_rate") or (coverage[0]["price"] if coverage else None),
                "base_fee": pricing.get("base_fee"),
                "per_km_fee": pricing.get("per_km_fee"),
            },
        })
    # No fallback "Siiqo Standard Delivery": when no partner covers an area,
    # checkout offers only the seller's own delivery or pickup.
    return jsonify({"status": "success", "data": partner_data}), 200


# ---------------------------------------------------------------------------
# GET /logistics/quote?vendor_id=&state=&city=&lga=  — checkout options,
# every fee priced by the server (the same prices checkout charges).
# ---------------------------------------------------------------------------

@logistics_bp.route('/quote', methods=['GET'])
def delivery_quote():
    try:
        vendor_id = int(request.args.get('vendor_id') or 0)
    except (TypeError, ValueError):
        vendor_id = 0
    sf = Storefront.query.filter_by(vendor_id=vendor_id).first() if vendor_id else None
    if sf is None:
        return jsonify({"message": "Store not found"}), 404
    state = (request.args.get('state') or '').strip()[:80]
    city = (request.args.get('city') or '').strip()[:80]
    lga = (request.args.get('lga') or '').strip()[:80] or None

    options = []
    try:
        fee = vendor_self_fee(sf, city, state)
        ds_public = public_delivery_settings(sf) or {}
        days = (ds_public.get("self_delivery") or {}).get("ships_within_days")
        options.append({"id": "vendor_self", "provider_type": "vendor", "provider_id": None,
                        "name": "Seller's own delivery", "type": "local_dispatch", "fee": fee,
                        "currency": "NGN",
                        "estimated_delivery": (f"Ships within {days} day{'s' if days != 1 else ''}"
                                               if days else "Seller will confirm timing")})
    except DeliveryError:
        pass
    # Pay on Delivery: only the seller's own delivery or pickup (delivery
    # partners need Safe Pay; app/services/delivery.py allowed_with_payment).
    pod = (request.args.get('payment_method') or '').strip().upper() == 'POD'
    if state and not pod:
        options.extend(q.public() for q in quote_options(sf, state, city, lga))

    ds = sf.delivery_settings if isinstance(sf.delivery_settings, dict) else None
    pickup = (ds or {}).get("pickup") or {}
    if ds is None or pickup.get("enabled"):
        area = (pickup.get("area") or ", ".join(x for x in (sf.city, sf.state) if x) or "").strip()
        options.append({"id": "self_pickup", "provider_type": "pickup", "provider_id": None,
                        "name": f"Pick up in {area} (free)" if area else "Pick up from the seller (free)",
                        "type": "pickup", "fee": 0.0, "currency": "NGN",
                        "estimated_delivery": "When ready"})
    body = {"status": "success", "vendor_id": vendor_id, "options": options}
    if pod:
        from app.services.delivery import POD_PARTNER_MESSAGE
        body["pod_note"] = POD_PARTNER_MESSAGE
    return jsonify(body), 200


# ---------------------------------------------------------------------------
# PATCH /logistics/assignments/<id>/status  {status, delivery_code, proof_photo_url}
# PATCH /logistics/assignments/<id>/deliver (older alias)
# Partner (or its assigned rider) moves a job: PICKED_UP → IN_TRANSIT → DELIVERED.
# ---------------------------------------------------------------------------

def _current_user():
    try:
        return db.session.get(User, int(get_jwt_identity()))
    except (TypeError, ValueError):
        return None


def _do_move(assignment_id: int):
    user = _current_user()
    if user is not None and user.role == UserRole.PARTNER:
        access = partner_access(user)
        if not access.ok:
            return access.response()
    assignment = db.session.get(LogisticsAssignment, assignment_id)
    if not assignment or actor_for(assignment, user) is None:
        return jsonify({"message": "Delivery not found"}), 404
    data = request.get_json(silent=True) or {}
    try:
        move(assignment, data.get('status') or 'DELIVERED', user, data)
    except FlowError as e:
        db.session.rollback()
        return e.response()
    return jsonify({"status": "success", "message": f"Delivery updated: {assignment.status}",
                    "assignment": assignment_public(assignment, for_rider=user.role == UserRole.RIDER)}), 200


@logistics_bp.route('/assignments/<int:assignment_id>/deliver', methods=['PATCH'])
@jwt_required()
def deliver_assignment(assignment_id):
    return _do_move(assignment_id)


@logistics_bp.route('/assignments/<int:assignment_id>/status', methods=['PATCH'])
@jwt_required()
def update_assignment_status(assignment_id):
    return _do_move(assignment_id)


# ---------------------------------------------------------------------------
# Proof of delivery photo: direct S3 upload (same helper as product files)
# ---------------------------------------------------------------------------

@logistics_bp.route('/assignments/<int:assignment_id>/proof-upload', methods=['POST'])
@jwt_required()
def proof_upload(assignment_id):
    """JSON {filename, file_type} → presigned S3 POST. Multipart 'file' → the
    upload helper saves it. Either way the answer has file_url to send with
    status DELIVERED as proof_photo_url."""
    assignment = db.session.get(LogisticsAssignment, assignment_id)
    user = _current_user()
    if not assignment or actor_for(assignment, user) is None:
        return jsonify({"message": "Delivery not found"}), 404
    from app.utils.upload import generate_presigned_upload_url, save_uploaded_file
    if 'file' in request.files:
        try:
            url = save_uploaded_file(request.files['file'], subfolder="delivery_proof", is_digital=False)
        except ValueError as e:
            return jsonify({"message": str(e)}), 400
        return jsonify({"status": "success", "file_url": url}), 200
    data = request.get_json(silent=True) or {}
    file_type = str(data.get('file_type') or 'image/jpeg')
    if not file_type.startswith('image/'):
        return jsonify({"message": "Only photos (JPG, PNG, WEBP) can be uploaded."}), 400
    try:
        res = generate_presigned_upload_url(filename=str(data.get('filename') or 'proof.jpg'),
                                            file_type=file_type, subfolder="delivery_proof", is_digital=False)
    except ValueError as e:
        return jsonify({"message": str(e)}), 400
    if not res.get("is_direct_s3"):
        res["fallback_upload_url"] = f"/api/logistics/assignments/{assignment_id}/proof-upload"
    return jsonify(res), 200


# ---------------------------------------------------------------------------
# GET /logistics/my-assignments  — partner's delivery jobs
# ---------------------------------------------------------------------------

@logistics_bp.route('/my-assignments', methods=['GET'])
@jwt_required()
@partner_required
def my_assignments():
    rows = LogisticsAssignment.query.filter(
        LogisticsAssignment.partner_id == g.partner.id, LogisticsAssignment.status != 'PENDING'
    ).order_by(LogisticsAssignment.created_at.desc()).limit(500).all()
    return jsonify({"status": "success", "assignments": [assignment_public(a) for a in rows]}), 200


# ---------------------------------------------------------------------------
# Partner riders / staff
# ---------------------------------------------------------------------------

def _temporary_password() -> str:
    alphabet = string.ascii_letters + string.digits
    while True:
        pw = "".join(secrets.choice(alphabet) for _ in range(12))
        if any(c.isdigit() for c in pw) and any(c.isupper() for c in pw) and any(c.islower() for c in pw):
            return pw


def _staff_user(staff):
    """The rider's login for a staff row (linked by id; older rows by email)."""
    if staff.user_id:
        return db.session.get(User, staff.user_id)
    if staff.staff_email:
        u = User.query.filter_by(email=staff.staff_email, role=UserRole.RIDER).first()
        if u is not None:
            staff.user_id = u.id
        return u
    return None


@logistics_bp.route('/staff', methods=['GET', 'POST'])
@jwt_required()
@partner_required
def manage_staff():
    partner = g.partner
    if request.method == 'GET':
        staff = PartnerStaff.query.filter_by(partner_id=partner.id, is_active=True).all()
        return jsonify({
            "status": "success",
            "staff": [{
                "id": s.id,
                "name": s.staff_name,
                "phone": s.staff_phone,
                "email": s.staff_email,
                "role": s.staff_role,
                "is_active": s.is_active,
                "has_login": bool(s.user_id or s.staff_email),
            } for s in staff],
        }), 200

    data = request.get_json(silent=True) or {}
    name = (data.get('staff_name') or data.get('name') or '').strip()[:100]
    phone = (data.get('staff_phone') or data.get('phone') or '').strip()[:20]
    email = (data.get('staff_email') or data.get('email') or '').strip().lower()[:120]

    if not name or not phone:
        return jsonify({"message": "Name and phone are required"}), 400
    if not email or '@' not in email:
        return jsonify({"message": "An email is required so the rider can log in"}), 400
    if User.query.filter(db.func.lower(User.email) == email).first():
        return jsonify({"message": "An account with this email already exists. Use another email."}), 409

    # The rider's login. The partner never picks the password: a random
    # temporary one is shown once, and the rider can set their own with
    # "Forgot password" (email code) at any time.
    temp_password = _temporary_password()
    names = name.split(' ', 1)
    rider_user = User(
        email=email,
        first_name=names[0],
        last_name=names[1] if len(names) > 1 else '',
        phone=phone,
        role=UserRole.RIDER,
        is_verified=True,
        is_active=True,
    )
    rider_user.set_password(temp_password)
    db.session.add(rider_user)
    db.session.flush()

    new_staff = PartnerStaff(
        partner_id=partner.id,
        user_id=rider_user.id,
        staff_name=name,
        staff_phone=phone,
        staff_email=email,
        staff_role=str(data.get('role') or 'RIDER').upper()[:50],
    )
    db.session.add(new_staff)
    db.session.commit()

    try:
        from app.utils import email as email_mod
        site = (os.environ.get('FRONTEND_URL') or 'https://siiqo.com').rstrip('/')
        business = (g.partner_app.business_name if g.partner_app else None) or partner.full_name
        email_mod.send_siiqo_email(
            to_email=email,
            subject="You've been added as a Siiqo rider",
            template_name="system_notice",
            first_name=names[0],
            notice_text=(f"{business} added you as a rider on Siiqo.<br><br>"
                         f"Log in at {site}/rider-login with this email. Your manager will give you a "
                         "temporary password, or tap <strong>Forgot password</strong> to set your own "
                         "with a code we email you."),
            cta_url=f"{site}/rider-login", cta_label="Open rider login",
        )
    except Exception as e:  # noqa: BLE001
        logging.getLogger(__name__).info("[LOGISTICS] rider invite email not sent: %s", e)

    return jsonify({
        "message": f"{name} added. Give them this temporary password; it is shown only once.",
        "id": new_staff.id,
        "temporary_password": temp_password,
        "login_email": email,
        "status": "success",
    }), 201


@logistics_bp.route('/staff/<int:staff_id>', methods=['PATCH', 'DELETE'])
@jwt_required()
@partner_required
def manage_staff_member(staff_id):
    staff = db.session.get(PartnerStaff, staff_id)
    if not staff or staff.partner_id != g.partner.id:
        return jsonify({"message": "Not found or unauthorized"}), 404

    if request.method == 'DELETE':
        staff.is_active = False
        rider = _staff_user(staff)
        if rider is not None and rider.role == UserRole.RIDER:
            rider.is_active = False
        db.session.commit()
        return jsonify({"message": "Staff member removed.", "status": "success"}), 200

    data = request.get_json(silent=True) or {}
    if 'name' in data:
        staff.staff_name = str(data['name'])[:100]
    if 'phone' in data:
        staff.staff_phone = str(data['phone'])[:20]
    if 'role' in data:
        staff.staff_role = str(data['role']).upper()[:50]
    if data.get('reset_password'):
        rider = _staff_user(staff)
        if rider is None or rider.role != UserRole.RIDER or not staff.is_active:
            return jsonify({"message": "This rider has no login to reset."}), 404
        temp_password = _temporary_password()
        rider.set_password(temp_password)
        db.session.commit()
        return jsonify({"message": "New temporary password made. It is shown only once.",
                        "temporary_password": temp_password, "status": "success"}), 200
    db.session.commit()
    return jsonify({"message": "Staff updated.", "status": "success"}), 200


@logistics_bp.route('/assignments/<int:assignment_id>/assign', methods=['PATCH'])
@jwt_required()
@partner_required
def assign_rider_to_assignment(assignment_id):
    assignment = db.session.get(LogisticsAssignment, assignment_id)
    if not assignment or assignment.partner_id != g.partner.id:
        return jsonify({"message": "Assignment not found or unauthorized"}), 404
    if assignment.status not in ('ASSIGNED', 'PICKED_UP', 'IN_TRANSIT'):
        return jsonify({"message": "You can choose a rider once the job is paid and before it is delivered."}), 409

    data = request.get_json(silent=True) or {}
    try:
        staff = db.session.get(PartnerStaff, int(data.get('staff_id')))
    except (TypeError, ValueError):
        return jsonify({"message": "staff_id is required"}), 400
    if not staff or staff.partner_id != g.partner.id or not staff.is_active:
        return jsonify({"message": "Staff member not found or inactive"}), 404
    rider = _staff_user(staff)

    assignment.rider_name = staff.staff_name
    assignment.rider_phone = staff.staff_phone
    assignment.staff_id = staff.id
    assignment.rider_user_id = rider.id if rider is not None and rider.role == UserRole.RIDER else None
    if assignment.rider_user_id:
        db.session.add(Notification(
            user_id=assignment.rider_user_id, title="New delivery",
            message=f"You have a new delivery: Order #{assignment.order_id}.", type="DELIVERY",
            order_id=assignment.order_id))
    db.session.commit()

    return jsonify({
        "message": f"Assigned {staff.staff_name} to delivery",
        "status": "success",
        "rider_name": staff.staff_name,
        "rider_phone": staff.staff_phone,
    }), 200


def _assignment_for_tracking(assignment_id):
    """By assignment id; older clients send the order id."""
    assignment = db.session.get(LogisticsAssignment, assignment_id)
    if not assignment:
        assignment = LogisticsAssignment.query.filter_by(order_id=assignment_id).first()
    return assignment


@logistics_bp.route('/assignments/<int:assignment_id>/telemetry', methods=['POST'])
@jwt_required()
def update_rider_telemetry(assignment_id):
    user = _current_user()
    assignment = _assignment_for_tracking(assignment_id)
    if not assignment or actor_for(assignment, user) is None:
        return jsonify({"message": "Assignment not found"}), 404
    if assignment.status not in ('ASSIGNED', 'PICKED_UP', 'IN_TRANSIT'):
        return jsonify({"message": "This delivery is not active"}), 409

    data = request.get_json(silent=True) or {}
    try:
        lat = float(data.get('latitude'))
        lng = float(data.get('longitude'))
    except (TypeError, ValueError):
        return jsonify({"message": "Invalid coordinates"}), 400
    if not (-90 <= lat <= 90 and -180 <= lng <= 180):
        return jsonify({"message": "Invalid coordinates"}), 400

    assignment.current_latitude = lat
    assignment.current_longitude = lng
    assignment.location_updated_at = datetime.now(timezone.utc)
    db.session.commit()
    return jsonify({"status": "success", "message": "Telemetry updated"}), 200


@logistics_bp.route('/assignments/<int:assignment_id>/location', methods=['GET'])
@jwt_required()
def get_rider_location(assignment_id):
    user = _current_user()
    if not user:
        return jsonify({"message": "User not found"}), 404
    assignment = _assignment_for_tracking(assignment_id)
    if not assignment:
        return jsonify({"message": "Assignment not found"}), 404

    order = assignment.order
    is_buyer = bool(order and order.buyer_id and order.buyer_id == user.id)
    is_vendor = bool(order and order.vendor_id == user.id)
    if not (is_buyer or is_vendor or actor_for(assignment, user)):
        return jsonify({"message": "Unauthorized"}), 403

    return jsonify({
        "status": "success",
        "assignment_id": assignment.id,
        "order_id": assignment.order_id,
        "rider_status": assignment.status,
        "latitude": float(assignment.current_latitude) if assignment.current_latitude is not None else None,
        "longitude": float(assignment.current_longitude) if assignment.current_longitude is not None else None,
        "location_updated_at": assignment.location_updated_at.isoformat() if assignment.location_updated_at else None,
    }), 200


# ---------------------------------------------------------------------------
# GET /logistics/partner/notifications  — the partner's delivery alerts
# ---------------------------------------------------------------------------

@logistics_bp.route('/partner/notifications', methods=['GET'])
@jwt_required()
@partner_required
def partner_notifications():
    rows = (Notification.query.filter_by(user_id=g.partner.id)
            .order_by(Notification.created_at.desc()).limit(50).all())
    return jsonify({"status": "success", "notifications": [{
        "id": n.id, "title": n.title, "message": n.message, "type": n.type, "is_read": bool(n.is_read),
        "order_id": n.order_id, "created_at": n.created_at.isoformat() if n.created_at else None,
    } for n in rows]}), 200


@logistics_bp.route('/partner/notifications/read', methods=['POST'])
@jwt_required()
@partner_required
def partner_notifications_read():
    Notification.query.filter_by(user_id=g.partner.id, is_read=False).update({"is_read": True})
    db.session.commit()
    return jsonify({"status": "success"}), 200
