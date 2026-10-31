"""
onboarding.py - AI seller onboarding API (Siiqo 2.0, Phase 1), /api/onboarding.

Contract: docs/PHASE_1_PLAN.md ("Backend API contract"). JWT on every route
except GET /name-check (public, rate-limited). A session belongs to one user;
any other user gets 404 for it.
"""
import logging
import os
import re
import uuid
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation

from flask import Blueprint, jsonify, request
from flask_jwt_extended import get_jwt_identity, jwt_required
from sqlalchemy import update
from sqlalchemy.exc import IntegrityError

from app.extensions import db, limiter
from app.models.jobs import Job
from app.models.onboarding import OnboardingPhoto, OnboardingSession, empty_stages
from app.models.user import User
from app.services import jobs
from app.services import onboarding_build as build
from app.services import onboarding_publish as publishing
from app.services.ai import AIError
from app.services.store_spec import parse_store_spec
from app.utils.upload import PHOTO_MAX_BYTES, looks_like_image, save_uploaded_file

onboarding_bp = Blueprint('onboarding', __name__)
log = logging.getLogger(__name__)

MAX_PHOTO_BYTES = PHOTO_MAX_BYTES
MAX_LISTINGS = 5
MAX_STEP = 7


def _env_int(name, default):
    try:
        return int(os.environ.get(name) or default)
    except ValueError:
        return default


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _get_user_id():
    """user id from the JWT; None for identities that are not plain user ids."""
    try:
        return int(get_jwt_identity())
    except (TypeError, ValueError):
        return None


def _error(http_status, message, code=None, **extra):
    body = {"message": message}
    if code:
        body["code"] = code
    body.update(extra)
    return jsonify(body), http_status


def _own_session(session_id):
    user_id = _get_user_id()
    if user_id is None:
        return None
    return OnboardingSession.query.filter_by(id=session_id, user_id=user_id).first()


def _open_session(user_id):
    return OnboardingSession.query.filter_by(open_user_id=user_id).first()


def _session_json(session):
    """Serialise for polling; a failed or stale build job puts the session back."""
    job = jobs.get(session.job_id) if session.job_id else None
    if session.status == 'building' and job is not None and job.status in ('failed', 'done'):
        build.recover_session(session, job)
        db.session.refresh(session)
    return session.to_dict(job=job)


# ---------------------------------------------------------------------------
# Answer validation
# ---------------------------------------------------------------------------

_PHONE_RE = re.compile(r"^\+?[0-9][0-9 ()\-]{6,19}$")

# key -> max length for plain text answers
_TEXT_ANSWERS = {
    'business_name': 100,
    'what_you_sell': 500,
    'category': 60,
    'who_buys': 300,
    'city': 100,
    'state': 100,
    'location': 120,
    'delivery': 300,
    'about': 1000,
}
PRODUCT_TYPES = ('physical', 'digital', 'service')
ANSWER_KEYS = tuple(_TEXT_ANSWERS) + ('store_slug', 'price_range', 'contact_phone', 'product_type')


def _validate_answers(raw):
    """Returns (clean, errors). A None or empty value means 'remove this answer'."""
    if not isinstance(raw, dict):
        return None, {"answers": "must be an object"}
    clean, errors = {}, {}
    for key, value in raw.items():
        if key not in ANSWER_KEYS:
            errors[key] = "unknown answer"
            continue
        if value is None or (isinstance(value, str) and not value.strip()):
            clean[key] = None
            continue
        if key in _TEXT_ANSWERS:
            if not isinstance(value, str):
                errors[key] = "must be text"
            elif len(value.strip()) > _TEXT_ANSWERS[key]:
                errors[key] = f"at most {_TEXT_ANSWERS[key]} characters"
            elif key == 'business_name' and len(value.strip()) < 2:
                errors[key] = "at least 2 characters"
            else:
                clean[key] = value.strip()
        elif key == 'store_slug':
            slug = value.strip().lower() if isinstance(value, str) else ''
            if not publishing.is_valid_slug(slug):
                errors[key] = "use lowercase letters, numbers and dashes (max 60)"
            elif slug in publishing._reserved():
                errors[key] = "this link is reserved"
            else:
                clean[key] = slug
        elif key == 'contact_phone':
            if not isinstance(value, str) or not _PHONE_RE.match(value.strip()):
                errors[key] = "enter a valid phone number"
            else:
                clean[key] = value.strip()
        elif key == 'product_type':
            if value not in PRODUCT_TYPES:
                errors[key] = f"one of {', '.join(PRODUCT_TYPES)}"
            else:
                clean[key] = value
        elif key == 'price_range':
            if isinstance(value, str):
                if len(value.strip()) > 60:
                    errors[key] = "at most 60 characters"
                else:
                    clean[key] = value.strip()
            elif isinstance(value, dict) and set(value) <= {'min', 'max'}:
                lo, hi = value.get('min'), value.get('max')
                ok = all(v is None or (isinstance(v, (int, float)) and not isinstance(v, bool) and v >= 0)
                         for v in (lo, hi))
                if not ok or (lo is not None and hi is not None and lo > hi):
                    errors[key] = "min and max must be numbers, min <= max"
                else:
                    clean[key] = {k: v for k, v in (('min', lo), ('max', hi)) if v is not None}
            else:
                errors[key] = "text or {min, max}"
    return clean, errors


def _merge_answers(current, clean):
    merged = dict(current or {})
    for key, value in clean.items():
        if value is None:
            merged.pop(key, None)
        else:
            merged[key] = value
    return merged


# ---------------------------------------------------------------------------
# GET /name-check  (public)
# ---------------------------------------------------------------------------

@onboarding_bp.route('/name-check', methods=['GET'])
@limiter.limit("30 per minute")
def name_check():
    name = (request.args.get('name') or '').strip()
    if not name:
        return _error(400, "name is required", "NAME_REQUIRED")
    if len(name) > 100:
        return _error(400, "name is too long (max 100 characters)", "NAME_TOO_LONG")
    return jsonify(publishing.check_name(name)), 200


# ---------------------------------------------------------------------------
# Sessions
# ---------------------------------------------------------------------------

@onboarding_bp.route('/sessions', methods=['POST'])
@jwt_required()
def create_session():
    user_id = _get_user_id()
    user = db.session.get(User, user_id) if user_id is not None else None
    if not user:
        return _error(404, "User not found")
    if user.storefront is not None:
        return _error(409, "You already have a storefront.", "ALREADY_HAS_STORE",
                      store_slug=user.storefront.store_slug)

    existing = _open_session(user.id)
    if existing:
        return jsonify({"session": _session_json(existing)}), 200

    data = request.get_json(silent=True) or {}
    answers = {}
    if data.get('answers') is not None:
        clean, errors = _validate_answers(data['answers'])
        if errors:
            return _error(400, "Some answers are not valid.", "INVALID_ANSWERS", errors=errors)
        answers = _merge_answers({}, clean)

    session = OnboardingSession(user_id=user.id, open_user_id=user.id, status='interview', step=1,
                                answers=answers, stages=empty_stages())
    db.session.add(session)
    try:
        db.session.commit()
    except IntegrityError:
        # Another request created it first: return that one.
        db.session.rollback()
        existing = _open_session(user.id)
        if existing:
            return jsonify({"session": _session_json(existing)}), 200
        raise
    return jsonify({"session": _session_json(session)}), 201


@onboarding_bp.route('/sessions/current', methods=['GET'])
@jwt_required()
def current_session():
    user_id = _get_user_id()
    session = _open_session(user_id) if user_id is not None else None
    if not session:
        return _error(404, "No open onboarding session", "NO_SESSION")
    return jsonify({"session": _session_json(session)}), 200


@onboarding_bp.route('/sessions/<int:session_id>', methods=['GET'])
@jwt_required()
def get_session(session_id):
    session = _own_session(session_id)
    if not session:
        return _error(404, "Session not found")
    return jsonify({"session": _session_json(session)}), 200


@onboarding_bp.route('/sessions/<int:session_id>', methods=['PATCH'])
@jwt_required()
def update_session(session_id):
    session = _own_session(session_id)
    if not session:
        return _error(404, "Session not found")
    if session.status not in ('interview', 'ready', 'ready_fallback'):
        return _error(409, f"Answers can't be changed while the session is {session.status}.",
                      "SESSION_LOCKED", status=session.status)

    data = request.get_json(silent=True)
    if not isinstance(data, dict) or not ({'answers', 'step'} & set(data)):
        return _error(400, "Send answers and/or step.", "NOTHING_TO_SAVE")

    errors = {}
    clean = {}
    if 'answers' in data:
        clean, errors = _validate_answers(data['answers'])
        errors = errors or {}
    step = data.get('step')
    if 'step' in data and (not isinstance(step, int) or isinstance(step, bool) or not 1 <= step <= MAX_STEP):
        errors['step'] = f"a number from 1 to {MAX_STEP}"
    if errors:
        return _error(400, "Some answers are not valid.", "INVALID_ANSWERS", errors=errors)

    if clean:
        session.answers = _merge_answers(session.answers, clean)
    if 'step' in data:
        session.step = step
    db.session.commit()
    return jsonify({"session": _session_json(session)}), 200


# ---------------------------------------------------------------------------
# Photos
# ---------------------------------------------------------------------------

_looks_like_image = looks_like_image   # shared with POST /api/vendor/products/draft-from-photo


@onboarding_bp.route('/sessions/<int:session_id>/photos', methods=['POST'])
@jwt_required()
def upload_photo(session_id):
    session = _own_session(session_id)
    if not session:
        return _error(404, "Session not found")
    if session.status not in ('interview', 'ready', 'ready_fallback'):
        return _error(409, f"Photos can't be changed while the session is {session.status}.",
                      "SESSION_LOCKED", status=session.status)

    file = request.files.get('photo') or request.files.get('file')
    if not file or not file.filename:
        return _error(400, "Attach a photo (multipart field 'photo').", "PHOTO_REQUIRED")
    kind = (request.form.get('kind') or 'product').strip().lower()
    if kind not in OnboardingPhoto.KINDS:
        return _error(400, "kind must be product or logo", "INVALID_KIND")

    caption = (request.form.get('caption') or '').strip() or None
    if caption and len(caption) > 200:
        return _error(400, "caption: at most 200 characters", "INVALID_CAPTION")
    price = None
    raw_price = (request.form.get('price') or '').strip()
    if raw_price:
        try:
            price = Decimal(raw_price)
            if not price.is_finite() or price < 0 or price > Decimal('99999999'):
                raise InvalidOperation
        except (InvalidOperation, ValueError):
            return _error(400, "price must be a positive number", "INVALID_PRICE")

    if kind == 'product' and len(session.product_photos()) >= build.MAX_PRODUCT_PHOTOS:
        return _error(400, f"You can add up to {build.MAX_PRODUCT_PHOTOS} product photos.", "PHOTO_LIMIT")

    file.stream.seek(0, 2)
    size = file.stream.tell()
    file.stream.seek(0)
    if size > MAX_PHOTO_BYTES:
        return _error(400, f"Photos can be up to {MAX_PHOTO_BYTES // (1024 * 1024)}MB.", "PHOTO_TOO_LARGE")
    head = file.stream.read(16)
    file.stream.seek(0)
    if not _looks_like_image(head):
        return _error(400, "Only JPG, PNG, WEBP or AVIF images are allowed.", "NOT_AN_IMAGE")

    try:
        url = save_uploaded_file(file, subfolder='onboarding')
    except ValueError as e:
        return _error(400, str(e), "UPLOAD_REJECTED")
    if not url:
        return _error(400, "Attach a photo (multipart field 'photo').", "PHOTO_REQUIRED")

    if kind == 'logo':
        old = session.logo()
        if old is not None:
            db.session.delete(old)
    photo = OnboardingPhoto(session_id=session.id, user_id=session.user_id, kind=kind, url=url,
                            caption=caption if kind == 'product' else None,
                            price=price if kind == 'product' else None)
    db.session.add(photo)
    db.session.commit()
    return jsonify({"photo": photo.to_dict()}), 201


@onboarding_bp.route('/sessions/<int:session_id>/photos/<int:photo_id>', methods=['DELETE'])
@jwt_required()
def delete_photo(session_id, photo_id):
    session = _own_session(session_id)
    if not session:
        return _error(404, "Session not found")
    if session.status not in ('interview', 'ready', 'ready_fallback'):
        return _error(409, f"Photos can't be changed while the session is {session.status}.",
                      "SESSION_LOCKED", status=session.status)
    photo = OnboardingPhoto.query.filter_by(id=photo_id, session_id=session.id).first()
    if not photo:
        return _error(404, "Photo not found")
    db.session.delete(photo)
    db.session.commit()
    return jsonify({"deleted": True}), 200


# ---------------------------------------------------------------------------
# Build
# ---------------------------------------------------------------------------

def _builds_today(user_id, job_type):
    """Builds that count against the daily cap: queued/running, or done after
    reaching the AI. Builds that never reached the AI (AI off) are free."""
    since = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    rows = Job.query.filter(Job.user_id == user_id, Job.type == job_type, Job.created_at >= since).all()
    return sum(1 for j in rows
               if j.status in ('queued', 'running') or (j.status == 'done' and (j.result or {}).get('ai_used')))


@onboarding_bp.route('/sessions/<int:session_id>/build', methods=['POST'])
@jwt_required()
def start_build(session_id):
    session = _own_session(session_id)
    if not session:
        return _error(404, "Session not found")
    data = request.get_json(silent=True) or {}
    part = data.get('part')
    if part is not None and part not in build.REBUILD_PARTS:
        return _error(400, f"part must be one of {', '.join(build.REBUILD_PARTS)}", "INVALID_PART")

    if session.status == 'building':
        _session_json(session)  # may recover a dead build
        if session.status == 'building':
            return _error(409, "A build is already running.", "BUILD_IN_PROGRESS",
                          job=jobs.get(session.job_id).to_dict() if session.job_id else None)
    if session.status == 'published':
        return _error(409, "This store is already published.", "SESSION_LOCKED", status=session.status)
    missing = build.missing_for_build(session.answers or {})
    if missing:
        return _error(400, "Answer these questions first.", "MISSING_ANSWERS", missing=missing)
    if part and session.status not in OnboardingSession.READY_STATUSES:
        return _error(400, "Build the whole store before rebuilding a part.", "NO_DRAFT_YET")

    job_type = build.JOB_TYPE_REBUILD if part else build.JOB_TYPE_BUILD
    cap = _env_int('ONBOARDING_DAILY_REBUILDS', 3) if part else _env_int('ONBOARDING_DAILY_BUILDS', 1)
    if _builds_today(session.user_id, job_type) >= cap:
        return _error(429, "You've reached today's limit for AI store builds. You can still edit your draft.",
                      "BUILD_LIMIT_REACHED", limit=cap)

    prev_status = session.status
    claimed = db.session.execute(
        update(OnboardingSession)
        .where(OnboardingSession.id == session.id, OnboardingSession.status == prev_status)
        .values(status='building')
        .execution_options(synchronize_session=False)
    ).rowcount == 1
    if not claimed:
        db.session.rollback()
        return _error(409, "The session changed; try again.", "BUILD_IN_PROGRESS")
    db.session.refresh(session)
    if part is None:
        session.stages = empty_stages()

    job = jobs.enqueue(job_type, {"session_id": session.id, "part": part, "prev_status": prev_status},
                       user_id=session.user_id)
    session = db.session.get(OnboardingSession, session_id)
    db.session.refresh(session)
    if session.job_id != job.id:
        session.job_id = job.id
        db.session.commit()
    job = jobs.get(job.id)
    return jsonify({"job": job.to_dict(), "session": _session_json(session)}), 202


# ---------------------------------------------------------------------------
# Draft edits
# ---------------------------------------------------------------------------

_SPEC_GROUPS = ('theme', 'hero', 'copy', 'nav', 'extra')


def _merge_spec(current, patch):
    merged = {k: (dict(v) if isinstance(v, dict) else (list(v) if isinstance(v, list) else v))
              for k, v in (current or {}).items()}
    for key, value in patch.items():
        if key in _SPEC_GROUPS and isinstance(value, dict) and isinstance(merged.get(key), dict):
            for sub, v in value.items():
                if v is None:
                    merged[key].pop(sub, None)
                else:
                    merged[key][sub] = v
        else:
            merged[key] = value
    return merged


def _validate_listings(raw, session):
    if not isinstance(raw, list):
        return None, ["draft_listings must be a list"]
    if len(raw) > MAX_LISTINGS:
        return None, [f"at most {MAX_LISTINGS} listings"]
    photos = {p.id: p for p in session.product_photos()}
    urls = {p.url for p in photos.values()}
    out, errors = [], []
    for i, item in enumerate(raw):
        where = f"draft_listings[{i}]"
        if not isinstance(item, dict):
            errors.append(f"{where}: must be an object")
            continue
        listing = {
            "id": str(item.get("id") or f"l{uuid.uuid4().hex[:8]}")[:40],
            "photo_id": item.get("photo_id"),
            "image_url": item.get("image_url"),
            "title": item.get("title") or "",
            "description": item.get("description") or "",
            "price": item.get("price"),
            "category": item.get("category"),
            "attributes": item.get("attributes") or [],
            "quantity": item.get("quantity"),
            "source": item.get("source") if item.get("source") in ("ai", "fallback", "seller") else "seller",
        }
        if listing["photo_id"] is not None and listing["photo_id"] not in photos:
            errors.append(f"{where}.photo_id: not a photo of this session")
        if listing["image_url"] is not None and listing["image_url"] not in urls:
            errors.append(f"{where}.image_url: must be one of this session's photos")
        for key, limit in (("title", 120), ("description", 2000)):
            if not isinstance(listing[key], str) or len(listing[key]) > limit:
                errors.append(f"{where}.{key}: text, at most {limit} characters")
        if listing["category"] is not None and (not isinstance(listing["category"], str) or len(listing["category"]) > 60):
            errors.append(f"{where}.category: text, at most 60 characters")
        price = listing["price"]
        if price is not None and (isinstance(price, bool) or not isinstance(price, (int, float))
                                  or price < 0 or price > 99999999):
            errors.append(f"{where}.price: a number from 0, or null")
        qty = listing["quantity"]
        if qty is not None and (isinstance(qty, bool) or not isinstance(qty, int) or qty < 0):
            errors.append(f"{where}.quantity: a whole number, or null")
        attrs = listing["attributes"]
        if (not isinstance(attrs, list) or len(attrs) > 10 or any(
                not isinstance(a, dict) or not isinstance(a.get("name"), str) or not isinstance(a.get("value"), str)
                or len(a["name"]) > 40 or len(a["value"]) > 100 for a in attrs)):
            errors.append(f"{where}.attributes: up to 10 {{name, value}} pairs")
        else:
            listing["attributes"] = [{"name": a["name"], "value": a["value"]} for a in attrs]
        listing["title"] = listing["title"].strip() if isinstance(listing["title"], str) else listing["title"]
        out.append(listing)
    return (None, errors) if errors else (out, [])


@onboarding_bp.route('/sessions/<int:session_id>/draft', methods=['PATCH'])
@jwt_required()
def update_draft(session_id):
    session = _own_session(session_id)
    if not session:
        return _error(404, "Session not found")
    if session.status not in OnboardingSession.READY_STATUSES:
        return _error(409, "The draft can be edited once the build is ready.", "BUILD_NOT_READY",
                      status=session.status)
    data = request.get_json(silent=True)
    if not isinstance(data, dict) or not ({'draft_spec', 'draft_listings', 'instruction'} & set(data)):
        return _error(400, "Send draft_spec, draft_listings and/or instruction.", "NOTHING_TO_SAVE")

    new_spec = session.draft_spec
    edit_summary = None
    if data.get('instruction') is not None:
        instruction = data['instruction']
        if not isinstance(instruction, str) or not instruction.strip() or len(instruction) > 500:
            return _error(400, "instruction: 1 to 500 characters", "INVALID_INSTRUCTION")
        try:
            new_spec, edit_summary = build.apply_chat_edit(session, instruction.strip())
        except build.EditRejected as exc:
            return _error(422, "That change can't be made automatically. Edit the text directly instead.",
                          "EDIT_REJECTED", problems=exc.problems[:10])
        except AIError as exc:
            log.info("onboarding chat edit unavailable: %s", exc)
            return _error(422, "Editing by message isn't available right now. Edit the text directly instead.",
                          "AI_EDIT_UNAVAILABLE")

    if 'draft_spec' in data:
        if not isinstance(data['draft_spec'], dict):
            return _error(400, "draft_spec must be an object", "INVALID_SPEC")
        merged = _merge_spec(new_spec, data['draft_spec'])
        parsed, errors = parse_store_spec(merged)
        if errors:
            return _error(400, "The store draft is not valid.", "INVALID_SPEC", errors=errors)
        new_spec = parsed

    listings = session.draft_listings
    if 'draft_listings' in data:
        listings, errors = _validate_listings(data['draft_listings'], session)
        if errors:
            return _error(400, "Some listings are not valid.", "INVALID_LISTINGS", errors=errors)

    session.draft_spec = new_spec
    session.draft_listings = listings
    db.session.commit()
    body = {"session": _session_json(session)}
    if edit_summary is not None:
        body["edit_summary"] = edit_summary
    return jsonify(body), 200


# ---------------------------------------------------------------------------
# Publish
# ---------------------------------------------------------------------------

@onboarding_bp.route('/sessions/<int:session_id>/publish', methods=['POST'])
@jwt_required()
def publish_session(session_id):
    session = _own_session(session_id)
    if not session:
        return _error(404, "Session not found")
    user = db.session.get(User, session.user_id)
    try:
        result, created = publishing.publish(session, user, request.get_json(silent=True) or {})
    except publishing.PublishError as exc:
        return jsonify(exc.body()), exc.status
    return jsonify(result), 201 if created else 200
