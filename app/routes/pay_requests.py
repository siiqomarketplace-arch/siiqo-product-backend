"""
Safe Pay requests (docs/SAFE_PAY_REQUEST_PLAN.md).

Public:  POST /api/pay-requests                  buyer creates (no account needed)
         GET  /api/pay-requests/<id>             status page (buyer and seller)
         POST /api/pay-requests/<id>/accept      seller (signed in) accepts
         POST /api/pay-requests/<id>/decline     seller declines
         POST /api/pay-requests/<id>/report      "not my business" / abuse
         POST /api/pay-requests/<id>/nudge       buyer, with the buyer token
Admin:   GET  /api/admin/pay-requests/most-wanted
"""
from flask import Blueprint, jsonify, request
from flask_jwt_extended import get_jwt_identity, jwt_required

from app.extensions import db, get_real_client_ip, limiter
from app.models.user import User
from app.services import pay_requests as svc

pay_requests_bp = Blueprint('pay_requests', __name__)
pay_requests_admin_bp = Blueprint('pay_requests_admin', __name__)


def _user():
    """The signed-in user, or None (admin tokens are not users)."""
    identity = get_jwt_identity()
    try:
        return db.session.get(User, int(identity)) if identity is not None else None
    except (TypeError, ValueError):
        return None


def _err(e: svc.PayRequestError):
    return jsonify(e.body()), e.status


def _not_found():
    return jsonify({"code": "NOT_FOUND", "message": "This request doesn't exist."}), 404


@pay_requests_bp.route('', methods=['POST'])
@pay_requests_bp.route('/', methods=['POST'])
@jwt_required(optional=True)
@limiter.limit("10 per hour;30 per day")
def create_request():
    body = request.get_json(silent=True) or {}
    user = _user()
    try:
        req, token = svc.create(body.get("seller"), item=body.get("item"), amount=body.get("amount"),
                                buyer_email=body.get("email") or (user.email if user else None),
                                buyer_user_id=user.id if user else None, ip=get_real_client_ip())
    except svc.PayRequestError as e:
        return _err(e)
    return jsonify({"request": svc.public(req), "buyer_token": token}), 201


@pay_requests_bp.route('/<public_id>', methods=['GET'])
@limiter.limit("60 per minute")
def get_request(public_id):
    req = svc.get(public_id)
    if req is None:
        return _not_found()
    return jsonify({"request": svc.public(req)}), 200


@pay_requests_bp.route('/<public_id>/accept', methods=['POST'])
@jwt_required()
@limiter.limit("10 per minute")
def accept_request(public_id):
    req = svc.get(public_id)
    user = _user()
    if req is None:
        return _not_found()
    if user is None:
        return jsonify({"code": "SIGN_IN_REQUIRED", "message": "Sign in to accept."}), 401
    try:
        result = svc.accept(req, user, request.get_json(silent=True) or {})
    except svc.PayRequestError as e:
        return _err(e)
    return jsonify({"request": result}), 200


def _close(public_id, outcome):
    req = svc.get(public_id)
    if req is None:
        return _not_found()
    body = request.get_json(silent=True) or {}
    try:
        return jsonify({"request": svc.close(req, outcome, body.get("reason"))}), 200
    except svc.PayRequestError as e:
        return _err(e)


@pay_requests_bp.route('/<public_id>/decline', methods=['POST'])
@limiter.limit("5 per minute")
def decline_request(public_id):
    return _close(public_id, "declined")


@pay_requests_bp.route('/<public_id>/report', methods=['POST'])
@limiter.limit("5 per minute")
def report_request(public_id):
    return _close(public_id, "reported")


@pay_requests_bp.route('/<public_id>/nudge', methods=['POST'])
@limiter.limit("10 per hour")
def nudge_request(public_id):
    req = svc.get(public_id)
    if req is None:
        return _not_found()
    body = request.get_json(silent=True) or {}
    try:
        return jsonify({"request": svc.nudge(req, str(body.get("buyer_token") or ""))}), 200
    except svc.PayRequestError as e:
        return _err(e)


@pay_requests_admin_bp.route('/most-wanted', methods=['GET'])
@jwt_required()
def most_wanted():
    from app.routes.admin import _get_admin, _parse_admin_id
    if not _get_admin(_parse_admin_id(get_jwt_identity())):
        return jsonify({"message": "Unauthorized"}), 403
    limit = request.args.get('limit', default=50, type=int)
    return jsonify({"businesses": svc.most_wanted(limit=limit)}), 200
