"""
Dispute routes for the buyer and the seller (Phase 2 item 2-5).

Registered on the escrow blueprint (imported at the bottom of
app/routes/escrow.py), so the URLs are /api/escrow/disputes/...:

    POST /api/escrow/disputes                     buyer opens a case (JSON or multipart with a photo)
    GET  /api/escrow/disputes/mine                cases where you are the buyer or the seller
    GET  /api/escrow/disputes/order/<order_id>    the case on one of your orders
    POST /api/escrow/disputes/<case_id>/evidence  add text and/or a photo (multipart or JSON)
    POST /api/escrow/disputes/<case_id>/submit    "I have finished my response"

The parties never see the reviewer summary or the reviewer's notes.
"""
from flask import jsonify, request
from flask_jwt_extended import get_jwt_identity, jwt_required

from app.extensions import db, limiter
from app.models.dispute import DisputeCase
from app.models.order import Order
from app.routes.escrow import escrow_bp
from app.services import disputes as svc
from app.utils.upload import save_uploaded_file


def _payload() -> dict:
    if request.is_json:
        return request.get_json(silent=True) or {}
    return request.form.to_dict()


def _photo_url():
    """Saves an uploaded image with the shared upload helper. Returns (url, error)."""
    file = request.files.get("photo") or request.files.get("file") or request.files.get("image")
    if not file or not file.filename:
        return None, None
    try:
        return save_uploaded_file(file, subfolder="dispute_evidence"), None
    except ValueError as e:
        return None, str(e)


def _err(e: svc.DisputeError):
    return jsonify(e.to_dict()), e.status


def _case_for_party(case_id: int, user_id):
    case = db.session.get(DisputeCase, case_id)
    if case is None or svc.party_of(case, user_id) is None:
        return None
    return case


@escrow_bp.route("/disputes", methods=["POST"])
@jwt_required()
@limiter.limit("10 per hour")
def open_dispute():
    user_id = get_jwt_identity()
    data = _payload()
    try:
        order_id = int(data.get("order_id") or 0)
    except (TypeError, ValueError):
        order_id = 0
    order = db.session.get(Order, order_id) if order_id else None
    if order is None or svc.party_of(order, user_id) is None:
        return jsonify({"message": "Order not found", "code": "not_found"}), 404
    photo_url, photo_error = _photo_url()
    if photo_error:
        return jsonify({"message": photo_error, "code": "bad_photo"}), 400
    try:
        case = svc.open_case(order, user_id, data.get("reason"), data.get("details"), photo_url=photo_url)
    except svc.DisputeError as e:
        return _err(e)
    return jsonify({"status": "success", "dispute": svc.party_view(case, user_id)}), 201


@escrow_bp.route("/disputes/by-token", methods=["POST"])
@limiter.limit("5 per hour")
def open_dispute_by_token():
    """"Report a problem" for a buyer without an account (Pay Link guests).
    Proof of being the buyer: their private order link token, or the code
    emailed to them (the same proofs that confirm delivery)."""
    import hmac
    from app.routes.escrow import _check_delivery_code, generate_order_token
    data = _payload()
    try:
        order_id = int(data.get("order_id") or 0)
    except (TypeError, ValueError):
        order_id = 0
    order = db.session.get(Order, order_id) if order_id else None
    if order is None:
        return jsonify({"message": "Order not found", "code": "not_found"}), 404
    token = str(data.get("token") or "").strip()
    code = str(data.get("code") or "").strip()
    proven = bool(token) and hmac.compare_digest(generate_order_token(order.id), token)
    if not proven:
        if not code:
            return jsonify({"message": "Request a code to your email to continue.",
                            "code": "BUYER_CODE_REQUIRED"}), 403
        ok, message = _check_delivery_code(order, code)
        if not ok:
            return jsonify({"message": message, "code": "BUYER_CODE_INVALID"}), 403
    photo_url, photo_error = _photo_url()
    if photo_error:
        return jsonify({"message": photo_error, "code": "bad_photo"}), 400
    try:
        case = svc.open_case(order, order.buyer_id, data.get("reason"), data.get("details"),
                             photo_url=photo_url, verified_buyer=True)
    except svc.DisputeError as e:
        return _err(e)
    return jsonify({"status": "success", "dispute": {"reference": case.reference, "status": case.status}}), 201


@escrow_bp.route("/disputes/mine", methods=["GET"])
@jwt_required()
def my_disputes():
    user_id = int(get_jwt_identity())
    svc.sweep_deadlines()
    cases = (DisputeCase.query
             .filter((DisputeCase.buyer_id == user_id) | (DisputeCase.vendor_id == user_id))
             .order_by(DisputeCase.created_at.desc()).limit(200).all())
    return jsonify({"status": "success", "disputes": [
        {k: v for k, v in svc.party_view(c, user_id).items() if k != "evidence"} for c in cases
    ]}), 200


@escrow_bp.route("/disputes/order/<int:order_id>", methods=["GET"])
@jwt_required()
def dispute_for_order(order_id):
    user_id = get_jwt_identity()
    order = db.session.get(Order, order_id)
    if order is None or svc.party_of(order, user_id) is None:
        return jsonify({"message": "Order not found", "code": "not_found"}), 404
    svc.sweep_deadlines()
    case = (DisputeCase.query.filter_by(order_id=order_id)
            .order_by(DisputeCase.created_at.desc(), DisputeCase.id.desc()).first())
    if case is None:
        return jsonify({"status": "success", "dispute": None}), 200
    return jsonify({"status": "success", "dispute": svc.party_view(case, user_id)}), 200


@escrow_bp.route("/disputes/<int:case_id>/evidence", methods=["POST"])
@jwt_required()
@limiter.limit("30 per hour")
def add_dispute_evidence(case_id):
    user_id = get_jwt_identity()
    case = _case_for_party(case_id, user_id)
    if case is None:
        return jsonify({"message": "Dispute not found", "code": "not_found"}), 404
    photo_url, photo_error = _photo_url()
    if photo_error:
        return jsonify({"message": photo_error, "code": "bad_photo"}), 400
    data = _payload()
    try:
        svc.add_evidence(case, user_id, text=data.get("text"), file_url=photo_url)
    except svc.DisputeError as e:
        return _err(e)
    return jsonify({"status": "success", "dispute": svc.party_view(case, user_id)}), 201


@escrow_bp.route("/disputes/<int:case_id>/submit", methods=["POST"])
@jwt_required()
def submit_dispute_response(case_id):
    user_id = get_jwt_identity()
    case = _case_for_party(case_id, user_id)
    if case is None:
        return jsonify({"message": "Dispute not found", "code": "not_found"}), 404
    try:
        svc.submit_response(case, user_id)
    except svc.DisputeError as e:
        return _err(e)
    case = db.session.get(DisputeCase, case_id)
    return jsonify({"status": "success", "dispute": svc.party_view(case, user_id)}), 200
