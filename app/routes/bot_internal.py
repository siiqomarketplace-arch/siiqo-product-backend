"""
Internal API for SiiqoBot (Telegram + WhatsApp). Not for browsers or the website.

Every route requires a valid X-Siiqo-Bot-Signature (app/services/bot_auth.py)
made with BOT_INTERNAL_SECRET; without the secret every route answers 503.

  GET    /api/internal/bot/sessions/<platform>/<user_id>   -> {payload, expires_at, updated_at} | 404
  PUT    /api/internal/bot/sessions/<platform>/<user_id>   {payload, ttl_seconds?} -> 200 | 413
  DELETE /api/internal/bot/sessions/<platform>/<user_id>   -> 200
  POST   /api/internal/bot/intent                          {platform, user_id, text}
  POST   /api/internal/bot/transcribe                      {platform, user_id, mime_type,
                                                            duration_seconds, audio_base64}

Sessions (T-2): the payload is an opaque string the bot has already encrypted.
It expires after ttl_seconds (default BOT_SESSION_TTL_SECONDS, capped by
BOT_SESSION_MAX_TTL_SECONDS) and may not exceed BOT_SESSION_MAX_BYTES.
"""
from __future__ import annotations

import base64
import binascii
import logging
import os
import re
from datetime import datetime, timedelta, timezone

from flask import Blueprint, jsonify, request
from sqlalchemy.exc import IntegrityError

from app.extensions import db, limiter
from app.models.bot import BotSession
from app.services import bot_intent, speech_to_text
from app.services.bot_auth import require_bot_signature

log = logging.getLogger(__name__)

bot_internal_bp = Blueprint("bot_internal", __name__)
# One bot IP carries every user's traffic, so the per-IP default limits do not
# fit. Each route is signature-gated and has its own per-user limits instead.
limiter.exempt(bot_internal_bp)

PLATFORMS = ("telegram", "whatsapp")
_USER_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")

DEFAULT_SESSION_TTL = 30 * 24 * 3600
DEFAULT_SESSION_MAX_BYTES = 64 * 1024
MIN_SESSION_TTL = 60

VOICE_MIME_TYPES = ("audio/ogg", "audio/opus", "audio/mpeg", "audio/mp4", "audio/aac",
                    "audio/amr", "audio/wav", "audio/x-wav", "audio/webm")
DEFAULT_VOICE_MAX_SECONDS = 60
DEFAULT_VOICE_MAX_BYTES = 1_000_000
MAX_TRANSCRIPT_CHARS = 1000


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name) or default)
    except ValueError:
        return default


def _now():
    return datetime.now(timezone.utc)


def _aware(dt):
    return dt.replace(tzinfo=timezone.utc) if dt is not None and dt.tzinfo is None else dt


def _bad(code: str, message: str, status: int = 400):
    return jsonify({"code": code, "message": message}), status


def _check_identity(platform: str, user_id: str):
    if platform not in PLATFORMS:
        return _bad("BAD_PLATFORM", "platform must be telegram or whatsapp")
    if not _USER_RE.match(user_id or ""):
        return _bad("BAD_USER", "user_id must be 1-64 characters of A-Z a-z 0-9 _ -")
    return None


# ── Sessions ────────────────────────────────────────────────────────────────
def _session_ttl(requested) -> int:
    max_ttl = _env_int("BOT_SESSION_MAX_TTL_SECONDS", DEFAULT_SESSION_TTL)
    default = min(_env_int("BOT_SESSION_TTL_SECONDS", DEFAULT_SESSION_TTL), max_ttl)
    if requested is None:
        return default
    try:
        ttl = int(requested)
    except (TypeError, ValueError):
        return default
    return max(MIN_SESSION_TTL, min(ttl, max_ttl))


def _session_out(row: BotSession):
    return jsonify({
        "payload": row.payload,
        "expires_at": _aware(row.expires_at).isoformat(),
        "updated_at": _aware(row.updated_at).isoformat() if row.updated_at else None,
    })


@bot_internal_bp.route("/sessions/<platform>/<user_id>", methods=["GET"])
@require_bot_signature
def get_session(platform, user_id):
    bad = _check_identity(platform, user_id)
    if bad:
        return bad
    row = BotSession.query.filter_by(platform=platform, user_ref=user_id).first()
    if row is None:
        return _bad("NOT_FOUND", "No session", 404)
    if _aware(row.expires_at) <= _now():
        db.session.delete(row)
        db.session.commit()
        return _bad("NOT_FOUND", "No session", 404)
    return _session_out(row), 200


@bot_internal_bp.route("/sessions/<platform>/<user_id>", methods=["PUT"])
@require_bot_signature
def put_session(platform, user_id):
    bad = _check_identity(platform, user_id)
    if bad:
        return bad
    body = request.get_json(silent=True)
    if not isinstance(body, dict) or not isinstance(body.get("payload"), str) or not body["payload"]:
        return _bad("BAD_PAYLOAD", "payload must be a non-empty string")
    payload = body["payload"]
    size = len(payload.encode("utf-8"))
    max_bytes = _env_int("BOT_SESSION_MAX_BYTES", DEFAULT_SESSION_MAX_BYTES)
    if size > max_bytes:
        return _bad("TOO_LARGE", f"session payload is {size} bytes; the limit is {max_bytes}", 413)

    now = _now()
    expires_at = now + timedelta(seconds=_session_ttl(body.get("ttl_seconds")))
    # Opportunistic clean-up of expired sessions (bounded, indexed on expires_at).
    expired_ids = [r.id for r in BotSession.query.with_entities(BotSession.id)
                   .filter(BotSession.expires_at <= now).limit(100).all()]
    if expired_ids:
        BotSession.query.filter(BotSession.id.in_(expired_ids)).delete(synchronize_session=False)

    row = BotSession.query.filter_by(platform=platform, user_ref=user_id).first()
    if row is None:
        row = BotSession(platform=platform, user_ref=user_id, created_at=now)
        db.session.add(row)
    row.payload, row.size_bytes, row.expires_at, row.updated_at = payload, size, expires_at, now
    try:
        db.session.commit()
    except IntegrityError:
        # Another worker created the row first: update it instead.
        db.session.rollback()
        row = BotSession.query.filter_by(platform=platform, user_ref=user_id).first()
        row.payload, row.size_bytes, row.expires_at, row.updated_at = payload, size, expires_at, now
        db.session.commit()
    return jsonify({"ok": True, "expires_at": expires_at.isoformat(), "size_bytes": size}), 200


@bot_internal_bp.route("/sessions/<platform>/<user_id>", methods=["DELETE"])
@require_bot_signature
def delete_session(platform, user_id):
    bad = _check_identity(platform, user_id)
    if bad:
        return bad
    BotSession.query.filter_by(platform=platform, user_ref=user_id).delete(synchronize_session=False)
    db.session.commit()
    return jsonify({"ok": True}), 200


# ── Intent ──────────────────────────────────────────────────────────────────
@bot_internal_bp.route("/intent", methods=["POST"])
@require_bot_signature
def intent():
    body = request.get_json(silent=True) or {}
    platform, user_id = str(body.get("platform") or ""), str(body.get("user_id") or "")
    bad = _check_identity(platform, user_id)
    if bad:
        return bad
    text = body.get("text")
    if not isinstance(text, str) or not text.strip():
        return _bad("BAD_TEXT", "text is required")
    if len(text) > bot_intent.MAX_TEXT_LEN * 4:
        return _bad("TOO_LONG", "text is too long", 413)
    result, status = bot_intent.classify(platform, user_id, text)
    return jsonify(result), status


# ── Voice notes ─────────────────────────────────────────────────────────────
@bot_internal_bp.route("/transcribe", methods=["POST"])
@require_bot_signature
def transcribe():
    if not speech_to_text.voice_enabled():
        return _bad("VOICE_DISABLED", "Voice notes are switched off.", 503)

    body = request.get_json(silent=True) or {}
    platform, user_id = str(body.get("platform") or ""), str(body.get("user_id") or "")
    bad = _check_identity(platform, user_id)
    if bad:
        return bad

    mime = str(body.get("mime_type") or "").split(";")[0].strip().lower()
    if mime not in VOICE_MIME_TYPES:
        return _bad("BAD_MIME", "Unsupported audio type.", 415)

    max_seconds = _env_int("VOICE_MAX_SECONDS", DEFAULT_VOICE_MAX_SECONDS)
    try:
        duration = float(body.get("duration_seconds"))
    except (TypeError, ValueError):
        return _bad("BAD_DURATION", "duration_seconds is required")
    if duration <= 0 or duration > max_seconds:
        return _bad("TOO_LONG", f"Voice notes can be up to {max_seconds} seconds.", 413)

    max_bytes = _env_int("VOICE_MAX_BYTES", DEFAULT_VOICE_MAX_BYTES)
    b64 = body.get("audio_base64")
    if not isinstance(b64, str) or not b64:
        return _bad("BAD_AUDIO", "audio_base64 is required")
    if len(b64) > (max_bytes * 4) // 3 + 8:          # reject before decoding
        return _bad("TOO_LARGE", "Voice note is too large.", 413)
    try:
        audio = base64.b64decode(b64, validate=True)
    except (binascii.Error, ValueError):
        return _bad("BAD_AUDIO", "audio_base64 is not valid base64")
    if not audio:
        return _bad("BAD_AUDIO", "audio is empty")
    if len(audio) > max_bytes:
        return _bad("TOO_LARGE", "Voice note is too large.", 413)

    limited = bot_intent.hit_rate_limit(
        "voice", platform, user_id,
        [("day", 86400, _env_int("VOICE_RATE_PER_DAY", 30)),
         ("minute", 60, _env_int("VOICE_RATE_PER_MINUTE", 5))],
    )
    if limited:
        return _bad("RATE_LIMITED", "Too many voice notes. Please type your message.", 429)

    provider = speech_to_text.get_provider()
    if provider is None:
        return _bad("VOICE_UNAVAILABLE", "Voice notes are not configured.", 503)

    ext = {"audio/ogg": "ogg", "audio/opus": "opus", "audio/mpeg": "mp3", "audio/mp4": "m4a",
           "audio/aac": "aac", "audio/amr": "amr", "audio/wav": "wav", "audio/x-wav": "wav",
           "audio/webm": "webm"}[mime]
    try:
        result = provider.transcribe(audio, mime, f"voice.{ext}")
    except speech_to_text.STTError as exc:
        log.warning("[BOT-VOICE] transcription failed (%s)", exc.reason)
        return _bad("VOICE_FAILED", "Could not transcribe the voice note.", 502)
    finally:
        del audio  # never kept beyond this request

    text = (result.text or "").strip()[:MAX_TRANSCRIPT_CHARS]
    if not text:
        return _bad("EMPTY_TRANSCRIPT", "No speech was recognised.", 422)
    return jsonify({"text": text, "language_code": result.language_code}), 200


# ── Log in with an email code (no password in chat) ─────────────────────────
#   POST /api/internal/bot/login-code/send    {platform, user_id, email}
#   POST /api/internal/bot/login-code/verify  {platform, user_id, email, code}
# The same answer whether or not the email has an account. On Telegram the
# verified account is linked to the Telegram id (as /auth/verify-link-otp does).
LOGIN_CODE_TTL = timedelta(minutes=10)
LOGIN_CODE_RESEND_AFTER = timedelta(seconds=60)
_EMAIL_RE = re.compile(r"^[^@\s]{1,64}@[^@\s]{1,120}\.[a-z]{2,24}$", re.I)


def _code_hash(user_id_db: int, code: str) -> str:
    import hashlib
    import hmac
    secret = (os.environ.get("SECRET_KEY") or "").encode()
    # users.reset_otp is VARCHAR(10): keep a 10-character keyed fingerprint, not the code.
    return hmac.new(secret, f"bot-login:{user_id_db}:{code}".encode(), hashlib.sha256).hexdigest()[:10]


@bot_internal_bp.route("/login-code/send", methods=["POST"])
@require_bot_signature
def login_code_send():
    import secrets
    from app.models.user import User
    body = request.get_json(silent=True) or {}
    platform, user_id = str(body.get("platform") or ""), str(body.get("user_id") or "")
    bad = _check_identity(platform, user_id)
    if bad:
        return bad
    email = str(body.get("email") or "").strip().lower()
    if not _EMAIL_RE.match(email):
        return _bad("BAD_EMAIL", "Type the email on your Siiqo account.")
    sent = {"status": "sent", "message": "If that email has a Siiqo account, we sent it a 6-digit code."}
    user = User.query.filter(db.func.lower(User.email) == email).first()
    if user is None or not getattr(user, "is_active", True):
        return jsonify(sent), 200
    now = _now()
    if user.otp_expiry and _aware(user.otp_expiry) - LOGIN_CODE_TTL + LOGIN_CODE_RESEND_AFTER > now:
        return jsonify({**sent, "status": "recently_sent"}), 200
    code = f"{secrets.randbelow(1_000_000):06d}"
    user.reset_otp = _code_hash(user.id, code)
    user.otp_expiry = now + LOGIN_CODE_TTL
    db.session.commit()
    try:
        from app.utils.email import send_siiqo_email
        send_siiqo_email(to_email=user.email, subject="Your Siiqo login code",
                         template_name="verify_email_otp", first_name=user.first_name or "there", otp=code)
    except Exception as e:  # noqa: BLE001
        log.warning("bot login code email failed: %s", e)
    return jsonify(sent), 200


@bot_internal_bp.route("/login-code/verify", methods=["POST"])
@require_bot_signature
def login_code_verify():
    import hmac
    from app.models.user import User
    from app.routes.auth import _make_tokens, _user_payload
    body = request.get_json(silent=True) or {}
    platform, user_id = str(body.get("platform") or ""), str(body.get("user_id") or "")
    bad = _check_identity(platform, user_id)
    if bad:
        return bad
    email = str(body.get("email") or "").strip().lower()
    code = re.sub(r"\D", "", str(body.get("code") or ""))
    user = User.query.filter(db.func.lower(User.email) == email).first() if email else None
    wrong = _bad("BAD_CODE", "That code didn't work. Check it, or ask for a new one.", 400)
    if user is None or len(code) != 6 or not user.reset_otp or not user.otp_expiry:
        return wrong
    if _aware(user.otp_expiry) < _now():
        return _bad("CODE_EXPIRED", "That code has expired. Ask for a new one.", 400)
    if not hmac.compare_digest(user.reset_otp, _code_hash(user.id, code)):
        return wrong
    user.reset_otp = None
    user.otp_expiry = None
    user.is_verified = True
    if platform == "telegram":
        other = User.query.filter(User.telegram_id == user_id, User.id != user.id).first()
        if other is None:
            user.telegram_id = user_id
    db.session.commit()
    return jsonify({"status": "success", "user": _user_payload(user), **_make_tokens(user)}), 200


# ── Safe Pay requests from chat ─────────────────────────────────────────────
#   POST /api/internal/bot/pay-requests  {platform, user_id, seller, item?, amount?, email?}
# Limits count per chat user (not per bot IP, which every user shares).
@bot_internal_bp.route("/pay-requests", methods=["POST"])
@require_bot_signature
def bot_pay_request():
    from app.services import pay_requests as pr
    body = request.get_json(silent=True) or {}
    platform, user_id = str(body.get("platform") or ""), str(body.get("user_id") or "")
    bad = _check_identity(platform, user_id)
    if bad:
        return bad
    try:
        req, token = pr.create(body.get("seller"), item=body.get("item"), amount=body.get("amount"),
                               buyer_email=body.get("email"), ip=f"bot:{platform}:{user_id}")
    except pr.PayRequestError as e:
        return jsonify(e.body()), e.status
    return jsonify({"request": pr.public(req), "buyer_token": token}), 201
