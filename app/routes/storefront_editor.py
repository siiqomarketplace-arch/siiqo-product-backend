"""
Store editor endpoints (Siiqo 2.0, Phase 1 items 1-6 and 1-8), on the vendor
blueprint (``/api/vendor``). Imported at the bottom of ``vendor.py``.

  GET  /storefront/revisions        the last 10 revisions of the seller's store
  POST /storefront/revert/<id>      keep the current state as a revision, then restore <id>
  POST /storefront/ai-edit          {spec, instruction} -> {proposal, summary} (preview only)
  POST /storefront/ai-refresh       {} -> {proposal, source, reason} (preview only)

Saving goes through the existing ``PATCH /update-settings`` with
``revision_source`` ("editor" or "ai_refresh"), which keeps a revision first.
Nothing here changes a live store except revert, which the seller taps.
"""
import logging

from flask import jsonify, request
from flask_jwt_extended import jwt_required

from app.extensions import db
from app.models.store_revision import StoreRevision
from app.routes.vendor import _get_user_id, _require_vendor_storefront, vendor_bp
from app.services import store_editor
from app.services.ai import AIError
from app.services.store_spec import parse_store_spec

log = logging.getLogger(__name__)

MAX_INSTRUCTION = 500


def _seller_store():
    user_id = _get_user_id()
    user, sf = _require_vendor_storefront(user_id)
    if not user:
        return None, None, (jsonify({"message": "Vendor access required"}), 403)
    if not sf:
        return user, None, (jsonify({"message": "Complete vendor onboarding first"}), 403)
    return user, sf, None


@vendor_bp.route('/storefront/revisions', methods=['GET'])
@jwt_required()
def storefront_revisions():
    user, sf, err = _seller_store()
    if err:
        return err
    revisions = store_editor.list_revisions(sf.id)
    return jsonify({"revisions": [r.to_dict() for r in revisions]}), 200


@vendor_bp.route('/storefront/revert/<int:revision_id>', methods=['POST'])
@jwt_required()
def storefront_revert(revision_id):
    user, sf, err = _seller_store()
    if err:
        return err
    rev = db.session.get(StoreRevision, revision_id)
    if rev is None or rev.storefront_id != sf.id:
        return jsonify({"message": "Revision not found"}), 404
    restored = store_editor.revert(sf, rev, user.id)
    db.session.commit()
    return jsonify({"status": "success", "template_options": restored}), 200


@vendor_bp.route('/storefront/ai-edit', methods=['POST'])
@jwt_required()
def storefront_ai_edit():
    user, sf, err = _seller_store()
    if err:
        return err
    data = request.get_json(silent=True) or {}
    instruction = data.get('instruction')
    if not isinstance(instruction, str) or not instruction.strip() or len(instruction) > MAX_INSTRUCTION:
        return jsonify({"message": f"instruction: 1 to {MAX_INSTRUCTION} characters",
                        "code": "INVALID_INSTRUCTION"}), 400
    spec, errors = parse_store_spec(data.get('spec'))
    if errors:
        return jsonify({"message": "The store design is not valid.", "code": "INVALID_SPEC",
                        "errors": errors[:10]}), 400
    try:
        proposal, summary = store_editor.ai_edit(sf, user.id, spec, instruction.strip())
    except store_editor.EditRejected as exc:
        return jsonify({"proposal": None, "reason": "rejected",
                        "message": "That change can't be made automatically. Edit the text directly instead.",
                        "problems": exc.problems[:10]}), 200
    except AIError as exc:
        log.info("store editor AI edit unavailable: %s", exc)
        return jsonify({"proposal": None, "reason": "ai_unavailable",
                        "message": "Changing your store by message isn't available right now. "
                                   "Edit the text directly instead."}), 200
    return jsonify({"proposal": proposal, "summary": summary}), 200


@vendor_bp.route('/storefront/ai-refresh', methods=['POST'])
@jwt_required()
def storefront_ai_refresh():
    user, sf, err = _seller_store()
    if err:
        return err
    result = store_editor.ai_refresh(sf, user.id)
    return jsonify(result), 200
