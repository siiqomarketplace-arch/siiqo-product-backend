import logging
"""
logistics.py — Logistics partner routes
"""
from datetime import datetime, timezone

from flask import Blueprint, request, jsonify
from flask_jwt_extended import jwt_required, get_jwt_identity

from app.extensions import db
from app.models.user import Storefront, User, UserRole
from app.models.escrow import LogisticsAssignment
from app.models.partnerships import PartnerApplication, PartnerStaff
from app.models.communication import Notification

logistics_bp = Blueprint('logistics', __name__)


def _utcnow():
    return datetime.now(timezone.utc)


# ---------------------------------------------------------------------------
# GET/POST /logistics/settings  — vendor logistics preferences
# ---------------------------------------------------------------------------

@logistics_bp.route('/settings', methods=['GET', 'POST'])
@jwt_required()
def vendor_logistics_settings():
    user_id = get_jwt_identity()
    user = db.session.get(User, int(user_id))
    if not user or not user.storefront:
        return jsonify({"message": "Vendor storefront required"}), 403

    sf = user.storefront

    if request.method == 'GET':
        return jsonify({
            "enabled_logistics": sf.logistics_settings or [],
            "store_city": sf.city,
            "store_state": sf.state,
        }), 200

    data = request.get_json() or {}
    sf.logistics_settings = data.get('enabled_logistics', [])
    if 'store_city' in data:
        sf.city = data['store_city']
    if 'store_state' in data:
        sf.state = data['store_state']
    db.session.commit()
    return jsonify({"message": "Logistics settings updated", "status": "success"}), 200


# ---------------------------------------------------------------------------
# GET /logistics/active  — public list of active logistics partners
# ---------------------------------------------------------------------------

@logistics_bp.route('/active', methods=['GET'])
def get_active_partners():
    role_filter = (request.args.get('role') or 'LOGISTICS').upper()
    state_filter = (request.args.get('state') or '').strip()

    partners = User.query.filter_by(role=UserRole.PARTNER, is_verified=True).all()
    partner_data = []

    for p in partners:
        app = PartnerApplication.query.filter_by(user_id=p.id, status='APPROVED').first()
        service_type = (app.service_type or 'LOGISTICS').upper() if app else 'LOGISTICS'

        if role_filter and service_type != role_filter:
            continue
        if state_filter and app and app.state_of_operation:
            if state_filter.lower() not in app.state_of_operation.lower():
                continue

        # Retrieve pricing settings or use sensible defaults
        pricing = app.pricing_settings if app and app.pricing_settings else {}
        store_settings = {
            "pricing_model": pricing.get("pricing_model") or "FLAT",
            "flat_rate": pricing.get("flat_rate") or 1500,
            "base_fee": pricing.get("base_fee") or 1000,
            "per_km_fee": pricing.get("per_km_fee") or 150,
            "external_api_key": pricing.get("external_api_key") or "",
        }

        partner_data.append({
            "id": p.id,
            "name": p.full_name,
            "email": p.email,
            "service_type": service_type,
            "partner_role": service_type,
            "state": app.state_of_operation if app else "Lagos",
            "status": "ACTIVE",
            "bank_code": app.bank_code if app else None,
            "account_number": app.account_number if app else None,
            "account_name": app.account_name if app else None,
            "store_settings": store_settings,
        })

    # Fallback: Siiqo default delivery option
    if not partner_data:
        partner_data = [{
            "id": 0,
            "name": "Siiqo Standard Delivery",
            "email": "logistics@siiqo.com",
            "service_type": "LOGISTICS",
            "partner_role": "LOGISTICS",
            "state": "Nationwide",
            "status": "ACTIVE",
            "store_settings": {
                "pricing_model": "FIXED",
                "base_fee": 1500,
                "per_km_fee": 0,
            },
        }]

    return jsonify({"status": "success", "data": partner_data}), 200


# ---------------------------------------------------------------------------
# PATCH /logistics/assignments/<id>/deliver  (primary route)
# PATCH /logistics/assignments/<id>/status   (alias — same logic)
# ---------------------------------------------------------------------------

def _do_deliver(assignment_id: int):
    """Shared logic for both /deliver and /status endpoints."""
    user_id = get_jwt_identity()
    assignment = db.session.get(LogisticsAssignment, assignment_id)
    if not assignment:
        return jsonify({"message": "Assignment not found"}), 404

    if assignment.partner_id != int(user_id):
        return jsonify({"message": "Unauthorized"}), 403

    data = request.get_json() or {}
    status = (data.get('status') or '').upper()

    if status not in ('IN_TRANSIT', 'DELIVERED', 'PENDING_PICKUP', 'REJECTED'):
        return jsonify({"message": "Status must be IN_TRANSIT, DELIVERED, PENDING_PICKUP, or REJECTED"}), 400

    # Secure OTP verification for escrow transactions
    if status == 'DELIVERED':
        if assignment.order and assignment.order.payment_method == 'ESCROW':
            escrow = assignment.order.escrow
            if escrow:
                provided_otp = str(data.get('delivery_otp') or '').strip()
                actual_otp = str(escrow.escrow_code or '').strip()
                if not actual_otp or provided_otp != actual_otp:
                    return jsonify({"message": "Invalid delivery OTP. Please verify with the buyer."}), 400

    assignment.status = status

    if status == 'DELIVERED':
        assignment.delivered_at = _utcnow()
        if assignment.order and assignment.order.escrow:
            assignment.order.escrow.status = 'DELIVERED'
            db.session.add(Notification(
                user_id=assignment.order.buyer_id,
                title="Your Order Has Been Delivered",
                message=(
                    f"Order #{assignment.order.id} has been delivered. "
                    "Please confirm receipt to release payment to the vendor."
                ),
                type="ORDER",
                order_id=assignment.order.id,
            ))

    db.session.commit()
    return jsonify({"message": f"Status updated to {status}", "status": "success"}), 200


@logistics_bp.route('/assignments/<int:assignment_id>/deliver', methods=['PATCH'])
@jwt_required()
def deliver_assignment(assignment_id):
    return _do_deliver(assignment_id)


@logistics_bp.route('/assignments/<int:assignment_id>/status', methods=['PATCH'])
@jwt_required()
def update_assignment_status(assignment_id):
    """Alias for /deliver — same logic, different URL expected by the dashboard."""
    return _do_deliver(assignment_id)


# ---------------------------------------------------------------------------
# GET /logistics/my-assignments  — partner's assigned deliveries
# ---------------------------------------------------------------------------

@logistics_bp.route('/my-assignments', methods=['GET'])
@jwt_required()
def my_assignments():
    user_id = get_jwt_identity()
    assignments = LogisticsAssignment.query.filter_by(partner_id=user_id).order_by(
        LogisticsAssignment.created_at.desc()
    ).all()

    return jsonify({
        "status": "success",
        "assignments": [{
            "id": a.id,
            "order_id": a.order_id,
            "status": a.status,
            "rider_name": a.rider_name,
            "rider_phone": a.rider_phone,
            "tracking_link": a.tracking_link,
            "delivery_fee": str(a.delivery_fee),
            "assigned_at": a.assigned_at.isoformat() if a.assigned_at else None,
            "delivered_at": a.delivered_at.isoformat() if a.delivered_at else None,
            "order": {
                "id": a.order.id,
                "total_amount": str(a.order.total_amount),
                "status": a.order.status,
                "buyer": {
                    "name": a.order.buyer.full_name if a.order.buyer else "Unknown",
                    "phone": a.order.buyer.phone if a.order.buyer else "",
                },
            } if a.order else None,
        } for a in assignments],
    }), 200


# ---------------------------------------------------------------------------
# Partner Staff Management
# ---------------------------------------------------------------------------

@logistics_bp.route('/staff', methods=['GET', 'POST'])
@jwt_required()
def manage_staff():
    user_id = get_jwt_identity()
    user = db.session.get(User, int(user_id))
    if not user or user.role != UserRole.PARTNER:
        return jsonify({"message": "Partner access required"}), 403

    if request.method == 'GET':
        staff = PartnerStaff.query.filter_by(partner_id=user_id, is_active=True).all()
        return jsonify({
            "status": "success",
            "staff": [{
                "id": s.id,
                "name": s.staff_name,
                "phone": s.staff_phone,
                "email": s.staff_email,
                "role": s.staff_role,
                "is_active": s.is_active,
            } for s in staff],
        }), 200

    data = request.get_json() or {}
    name = (data.get('staff_name') or data.get('name') or '').strip()
    phone = (data.get('staff_phone') or data.get('phone') or '').strip()
    email = (data.get('staff_email') or data.get('email') or '').strip().lower()
    password = data.get('password') or ''

    if not name or not phone:
        return jsonify({"message": "Name and phone are required"}), 400

    if not email:
        return jsonify({"message": "Email is required to create a rider login"}), 400

    # Ensure no existing standard user account has this email
    existing_user = User.query.filter_by(email=email).first()
    if existing_user:
        return jsonify({"message": f"An account with email {email} already exists"}), 409

    # Create standard User account for the Rider (Option A)
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
    if password:
        rider_user.set_password(password)
    else:
        rider_user.set_password("SiiqoRiderTempPass123!") # Fallback
    
    db.session.add(rider_user)
    db.session.flush()

    new_staff = PartnerStaff(
        partner_id=user_id,
        staff_name=name,
        staff_phone=phone,
        staff_email=email,
        staff_role=(data.get('role') or 'RIDER').upper(),
    )
    db.session.add(new_staff)
    db.session.commit()
    return jsonify({
        "message": f"{name} added to your team and user account created.",
        "id": new_staff.id,
        "status": "success",
    }), 201


@logistics_bp.route('/staff/<int:staff_id>', methods=['PATCH', 'DELETE'])
@jwt_required()
def manage_staff_member(staff_id):
    user_id = get_jwt_identity()
    staff = db.session.get(PartnerStaff, staff_id)
    if not staff or staff.partner_id != int(user_id):
        return jsonify({"message": "Not found or unauthorized"}), 404

    if request.method == 'DELETE':
        staff.is_active = False
        # Deactivate standard User account
        if staff.staff_email:
            user_rec = User.query.filter_by(email=staff.staff_email).first()
            if user_rec:
                user_rec.is_active = False
        db.session.commit()
        return jsonify({"message": "Staff member removed.", "status": "success"}), 200

    data = request.get_json() or {}
    if 'name' in data:
        staff.staff_name = data['name']
    if 'phone' in data:
        staff.staff_phone = data['phone']
    if 'role' in data:
        staff.staff_role = data['role'].upper()
    db.session.commit()
    return jsonify({"message": "Staff updated.", "status": "success"}), 200


@logistics_bp.route('/assignments/<int:assignment_id>/assign', methods=['PATCH'])
@jwt_required()
def assign_rider_to_assignment(assignment_id):
    user_id = get_jwt_identity()
    user = db.session.get(User, int(user_id))
    if not user or user.role != UserRole.PARTNER:
        return jsonify({"message": "Partner access required"}), 403

    assignment = db.session.get(LogisticsAssignment, assignment_id)
    if not assignment or assignment.partner_id != int(user_id):
        return jsonify({"message": "Assignment not found or unauthorized"}), 404

    data = request.get_json() or {}
    staff_id = data.get('staff_id')
    if not staff_id:
        return jsonify({"message": "staff_id is required"}), 400

    staff = db.session.get(PartnerStaff, int(staff_id))
    if not staff or staff.partner_id != int(user_id) or not staff.is_active:
        return jsonify({"message": "Staff member not found or inactive"}), 404

    assignment.rider_name = staff.staff_name
    assignment.rider_phone = staff.staff_phone
    assignment.status = 'ASSIGNED'
    db.session.commit()

    return jsonify({
        "message": f"Assigned {staff.staff_name} to delivery",
        "status": "success",
        "rider_name": staff.staff_name,
        "rider_phone": staff.staff_phone
    }), 200
