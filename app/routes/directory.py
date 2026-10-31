"""
Business directory and claim flow (Phase 3 item 3-5). See app/services/directory.py.

Public (/api/directory):
    GET  /listings/<id>                      public fields + the ways to prove ownership
    POST /listings/<id>/claim                start a claim (signed-in user) {method}
    POST /listings/<id>/removal              ask for removal (anyone) {method, reason}
    GET  /claims/<public_id>                 status, instructions, next step
    POST /claims/<public_id>/code            phone: submit the code {code}
    POST /claims/<public_id>/check           domain/handle: run the check (job runner)
    POST /claims/<public_id>/link            link a claimed listing to the store created since

Admin (/api/admin/directory):
    POST /import/dry-run                     CSV upload (superadmin) {file|csv, source, source_note}
    GET  /import/<batch_id>                  the dry run (any admin)
    POST /import/<batch_id>/apply            apply a dry run once (superadmin, audit-logged)
    GET  /listings?status=&q=                list (any admin)
    POST /listings/<id>/remove               remove (superadmin, audit-logged) {reason}
    GET  /audit?listing_id=&batch_id=        directory audit events (any admin)
"""
from flask import Blueprint, jsonify, request
from flask_jwt_extended import get_jwt_identity, jwt_required

from app.extensions import db, limiter
from app.models.directory import (BusinessListing, DirectoryAuditEvent, DirectoryClaim, DirectoryImportBatch,
                                  ListingStatus)
from app.services import directory as svc

directory_bp = Blueprint('directory', __name__)
directory_admin_bp = Blueprint('directory_admin', __name__)


def _ip():
    from app.routes.admin import _client_ip
    try:
        return _client_ip()
    except Exception:  # noqa: BLE001
        return None


def _user_id():
    """The signed-in user's id, or None (admin tokens are not users)."""
    identity = get_jwt_identity()
    if identity is None:
        return None
    try:
        return int(identity)
    except (TypeError, ValueError):
        return None


def _err(e):
    return jsonify(e.body()), e.status


def _listing_or_404(listing_id):
    listing = db.session.get(BusinessListing, listing_id)
    if listing is None or listing.status == ListingStatus.REMOVED:
        return None
    return listing


def _claim_or_404(public_id):
    return DirectoryClaim.query.filter_by(public_id=(public_id or "")[:32]).first()


# ── public ─────────────────────────────────────────────────────────────────

@directory_bp.route('/listings/<int:listing_id>', methods=['GET'])
@limiter.limit("60 per minute")
def get_listing(listing_id):
    listing = _listing_or_404(listing_id)
    if listing is None:
        return jsonify({"code": "NOT_FOUND", "message": "Listing not found."}), 404
    return jsonify({"listing": svc.claim_options(listing)}), 200


@directory_bp.route('/listings/<int:listing_id>/claim', methods=['POST'])
@jwt_required()
@limiter.limit("10 per minute")
def start_claim(listing_id):
    user_id = _user_id()
    if user_id is None:
        return jsonify({"code": "SIGN_IN_REQUIRED", "message": "Sign in to claim this business."}), 401
    listing = _listing_or_404(listing_id)
    if listing is None:
        return jsonify({"code": "NOT_FOUND", "message": "Listing not found."}), 404
    body = request.get_json(silent=True) or {}
    try:
        claim = svc.start(listing, purpose="claim", method=str(body.get("method") or "").strip().lower(),
                          user_id=user_id, ip=_ip())
    except svc.DirectoryError as e:
        return _err(e)
    return jsonify({"claim": svc.claim_public(claim)}), 201


@directory_bp.route('/listings/<int:listing_id>/removal', methods=['POST'])
@jwt_required(optional=True)
@limiter.limit("5 per minute")
def start_removal(listing_id):
    listing = _listing_or_404(listing_id)
    if listing is None:
        return jsonify({"code": "NOT_FOUND", "message": "Listing not found."}), 404
    body = request.get_json(silent=True) or {}
    try:
        claim = svc.start(listing, purpose="removal", method=str(body.get("method") or "").strip().lower(),
                          user_id=_user_id(), reason=body.get("reason"), ip=_ip())
    except svc.DirectoryError as e:
        return _err(e)
    return jsonify({"claim": svc.claim_public(claim)}), 201


def _may_see(claim):
    """Claims belong to their user; removal requests are reachable by their
    unguessable id (the requester may have no account)."""
    if claim.purpose == "claim":
        return claim.user_id is not None and claim.user_id == _user_id()
    return True


@directory_bp.route('/claims/<public_id>', methods=['GET'])
@jwt_required(optional=True)
@limiter.limit("60 per minute")
def get_claim(public_id):
    claim = _claim_or_404(public_id)
    if claim is None or not _may_see(claim):
        return jsonify({"code": "NOT_FOUND", "message": "Request not found."}), 404
    return jsonify({"claim": svc.claim_public(claim)}), 200


@directory_bp.route('/claims/<public_id>/code', methods=['POST'])
@jwt_required(optional=True)
@limiter.limit("10 per minute")
def submit_code(public_id):
    claim = _claim_or_404(public_id)
    if claim is None or not _may_see(claim):
        return jsonify({"code": "NOT_FOUND", "message": "Request not found."}), 404
    body = request.get_json(silent=True) or {}
    try:
        svc.verify_code(claim, body.get("code"), ip=_ip())
    except svc.DirectoryError as e:
        return _err(e)
    return jsonify({"claim": svc.claim_public(claim)}), 200


@directory_bp.route('/claims/<public_id>/check', methods=['POST'])
@jwt_required(optional=True)
@limiter.limit("6 per minute")
def run_check(public_id):
    claim = _claim_or_404(public_id)
    if claim is None or not _may_see(claim):
        return jsonify({"code": "NOT_FOUND", "message": "Request not found."}), 404
    try:
        svc.request_check(claim)
    except svc.DirectoryError as e:
        return _err(e)
    return jsonify({"claim": svc.claim_public(claim)}), 202


@directory_bp.route('/claims/<public_id>/link', methods=['POST'])
@jwt_required()
@limiter.limit("10 per minute")
def link_claim(public_id):
    claim = _claim_or_404(public_id)
    if claim is None or claim.purpose != "claim" or not _may_see(claim):
        return jsonify({"code": "NOT_FOUND", "message": "Request not found."}), 404
    listing = db.session.get(BusinessListing, claim.listing_id)
    if claim.status != "verified" or listing is None:
        return jsonify({"code": "NOT_VERIFIED", "message": "Prove ownership first."}), 409
    sf = svc.link_storefront(listing, claim.user_id)
    db.session.commit()
    if sf is None:
        return jsonify({"code": "NO_STORE", "message": "Create your store first.",
                        "claim": svc.claim_public(claim)}), 409
    return jsonify({"claim": svc.claim_public(claim)}), 200


# ── admin ──────────────────────────────────────────────────────────────────

def _admin():
    from app.routes.admin import _get_admin, _parse_admin_id
    return _get_admin(_parse_admin_id(get_jwt_identity()))


def _superadmin():
    from app.routes.admin import _parse_admin_id, _require_superadmin
    return _require_superadmin(_parse_admin_id(get_jwt_identity()))


def _forbidden():
    return jsonify({"message": "Unauthorized"}), 403


@directory_admin_bp.route('/import/dry-run', methods=['POST'])
@jwt_required()
def import_dry_run():
    admin = _superadmin()
    if not admin:
        return _forbidden()
    upload = request.files.get('file')
    if upload is not None:
        raw = upload.read(svc.MAX_IMPORT_BYTES + 1)
        text = raw.decode('utf-8', 'replace')
        form = request.form
        filename = upload.filename
    else:
        form = request.get_json(silent=True) or {}
        text = str(form.get('csv') or '')
        filename = form.get('filename')
    try:
        batch = svc.dry_run(admin, text, source=form.get('source'), source_note=form.get('source_note'),
                            filename=filename, ip=_ip())
    except svc.DirectoryError as e:
        return _err(e)
    return jsonify({"batch": svc.batch_public(batch)}), 201


@directory_admin_bp.route('/import/<int:batch_id>', methods=['GET'])
@jwt_required()
def import_get(batch_id):
    if not _admin():
        return _forbidden()
    batch = db.session.get(DirectoryImportBatch, batch_id)
    if batch is None:
        return jsonify({"code": "NOT_FOUND", "message": "Import not found."}), 404
    return jsonify({"batch": svc.batch_public(batch)}), 200


@directory_admin_bp.route('/import/<int:batch_id>/apply', methods=['POST'])
@jwt_required()
def import_apply(batch_id):
    admin = _superadmin()
    if not admin:
        return _forbidden()
    try:
        batch, added = svc.apply(admin, batch_id, ip=_ip())
    except svc.DirectoryError as e:
        return _err(e)
    return jsonify({"batch": svc.batch_public(batch), "added": len(added)}), 200


def _admin_listing(listing):
    return {**svc.public_listing(listing), "phone": listing.phone, "website": listing.website,
            "social_network": listing.social_network, "social_handle": listing.social_handle,
            "source": listing.source, "source_note": listing.source_note,
            "import_batch_id": listing.import_batch_id, "claimed_storefront_id": listing.claimed_storefront_id,
            "removed_reason": listing.removed_reason,
            "created_at": listing.created_at.isoformat() if listing.created_at else None}


@directory_admin_bp.route('/listings', methods=['GET'])
@jwt_required()
def admin_listings():
    if not _admin():
        return _forbidden()
    q = BusinessListing.query
    status = (request.args.get('status') or '').strip().lower()
    if status in ListingStatus.ALL:
        q = q.filter(BusinessListing.status == status)
    term = svc.name_key(request.args.get('q') or '')
    if term:
        q = q.filter(BusinessListing.name_key.contains(term, autoescape=True))
    rows = q.order_by(BusinessListing.id.desc()).limit(200).all()
    counts = {s: BusinessListing.query.filter_by(status=s).count() for s in ListingStatus.ALL}
    return jsonify({"listings": [_admin_listing(r) for r in rows], "counts": counts}), 200


@directory_admin_bp.route('/listings/<int:listing_id>/remove', methods=['POST'])
@jwt_required()
def admin_remove(listing_id):
    admin = _superadmin()
    if not admin:
        return _forbidden()
    listing = db.session.get(BusinessListing, listing_id)
    if listing is None:
        return jsonify({"code": "NOT_FOUND", "message": "Listing not found."}), 404
    body = request.get_json(silent=True) or {}
    try:
        svc.admin_remove(admin, listing, body.get('reason'), ip=_ip())
    except svc.DirectoryError as e:
        return _err(e)
    return jsonify({"listing": _admin_listing(listing)}), 200


@directory_admin_bp.route('/audit', methods=['GET'])
@jwt_required()
def admin_audit():
    if not _admin():
        return _forbidden()
    q = DirectoryAuditEvent.query
    for arg, col in (('listing_id', DirectoryAuditEvent.listing_id), ('batch_id', DirectoryAuditEvent.batch_id)):
        value = request.args.get(arg, type=int)
        if value:
            q = q.filter(col == value)
    rows = q.order_by(DirectoryAuditEvent.id.desc()).limit(200).all()
    return jsonify({"events": [{"id": r.id, "event": r.event, "listing_id": r.listing_id, "batch_id": r.batch_id,
                                "claim_id": r.claim_id, "actor_type": r.actor_type, "actor_id": r.actor_id,
                                "details": r.details,
                                "created_at": r.created_at.isoformat() if r.created_at else None}
                               for r in rows]}), 200
