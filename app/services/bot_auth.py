"""
Signed bot -> backend requests (Phase T / W internal API).

The bot (SiiqoBot on Render) signs every call to /api/internal/bot/* with the
shared secret BOT_INTERNAL_SECRET. There is no default: when the secret is
unset (or shorter than MIN_SECRET_LEN) every internal endpoint answers 503, so
the API fails closed.

Header (one line):

    X-Siiqo-Bot-Signature: t=<unix seconds>,n=<nonce>,v1=<hex HMAC-SHA256>

The MAC covers, joined by "\\n":  t, nonce, HTTP method, path (+ "?" + query
when there is one), then the raw body bytes. Binding the method and path means
a signature for GET /sessions/... cannot be replayed against PUT or another
user's path.

Replay protection:
  * |now - t| must be within BOT_INTERNAL_MAX_SKEW_SECONDS (default 300);
  * for writes (anything but GET/HEAD) the nonce is stored in
    bot_request_nonces and a second use inside the window is refused (409).
    Rows older than the window are pruned as new nonces arrive.

The bot-side implementation is SiiqoBot/internalApi.js (same canonical string).
"""
from __future__ import annotations

import functools
import hashlib
import hmac
import logging
import os
import re
import time
from datetime import datetime, timedelta, timezone

from flask import jsonify, request
from sqlalchemy.exc import IntegrityError

from app.extensions import db
from app.models.bot import BotRequestNonce

log = logging.getLogger(__name__)

SIGNATURE_HEADER = "X-Siiqo-Bot-Signature"
MIN_SECRET_LEN = 32
DEFAULT_MAX_SKEW = 300
_NONCE_RE = re.compile(r"^[A-Za-z0-9_-]{16,64}$")


def _secret() -> str:
    raw = (os.environ.get("BOT_INTERNAL_SECRET") or "").strip()
    return raw if len(raw) >= MIN_SECRET_LEN else ""


def max_skew_seconds() -> int:
    try:
        return max(30, int(os.environ.get("BOT_INTERNAL_MAX_SKEW_SECONDS") or DEFAULT_MAX_SKEW))
    except ValueError:
        return DEFAULT_MAX_SKEW


def canonical_string(timestamp: int, nonce: str, method: str, path: str, body: bytes) -> bytes:
    head = f"{int(timestamp)}\n{nonce}\n{method.upper()}\n{path}\n".encode("utf-8")
    return head + (body or b"")


def sign(secret: str, timestamp: int, nonce: str, method: str, path: str, body: bytes) -> str:
    """Header value for a request (used by tests; the bot has its own copy)."""
    mac = hmac.new(secret.encode("utf-8"), canonical_string(timestamp, nonce, method, path, body),
                   hashlib.sha256).hexdigest()
    return f"t={int(timestamp)},n={nonce},v1={mac}"


def _parse(header: str):
    try:
        parts = dict(p.split("=", 1) for p in (header or "").split(","))
        return int(parts["t"]), parts["n"], parts["v1"]
    except (ValueError, KeyError):
        return None


def _request_path() -> str:
    qs = request.query_string.decode("latin-1") if request.query_string else ""
    return request.path + (f"?{qs}" if qs else "")


def _remember_nonce(nonce: str, window: int) -> bool:
    """Insert the nonce; False if it was already used. Prunes expired rows."""
    now = datetime.now(timezone.utc)
    try:
        BotRequestNonce.query.filter(
            BotRequestNonce.seen_at < now - timedelta(seconds=window * 2)
        ).delete(synchronize_session=False)
        db.session.add(BotRequestNonce(nonce=nonce, seen_at=now))
        db.session.commit()
        return True
    except IntegrityError:
        db.session.rollback()
        return False


def verify_current_request(now: float | None = None):
    """(ok, error_response). Call inside a request context."""
    secret = _secret()
    if not secret:
        log.error("[BOT-INTERNAL] BOT_INTERNAL_SECRET is not set (or shorter than %d chars); "
                  "internal bot API refused.", MIN_SECRET_LEN)
        return False, (jsonify({"code": "BOT_INTERNAL_DISABLED",
                                "message": "Internal bot API is not configured."}), 503)

    parsed = _parse(request.headers.get(SIGNATURE_HEADER, ""))
    if not parsed or not _NONCE_RE.match(parsed[1]):
        return False, (jsonify({"code": "BAD_SIGNATURE", "message": "Unauthorized"}), 401)
    ts, nonce, given = parsed

    window = max_skew_seconds()
    now = time.time() if now is None else now
    if abs(now - ts) > window:
        return False, (jsonify({"code": "STALE_SIGNATURE", "message": "Unauthorized"}), 401)

    expected = hmac.new(
        secret.encode("utf-8"),
        canonical_string(ts, nonce, request.method, _request_path(), request.get_data(cache=True)),
        hashlib.sha256,
    ).hexdigest()
    if not hmac.compare_digest(expected, given):
        log.warning("[BOT-INTERNAL] rejected request with invalid signature (path=%s)", request.path)
        return False, (jsonify({"code": "BAD_SIGNATURE", "message": "Unauthorized"}), 401)

    if request.method not in ("GET", "HEAD") and not _remember_nonce(nonce, window):
        log.warning("[BOT-INTERNAL] rejected replayed nonce (path=%s)", request.path)
        return False, (jsonify({"code": "REPLAYED", "message": "Request already processed"}), 409)
    return True, None


def signed_by_bot():
    """
    For public routes that accept an optional bot-only field (telegram_id on
    /api/auth/register, /login, /verify-email, /send-link-otp, /verify-link-otp).

    Returns (signed, error_response):
      (False, None) - no signature header, or BOT_INTERNAL_SECRET unset (fail
                      closed: the caller ignores the bot-only field);
      (True,  None) - valid, fresh, first-use signature from the bot;
      (False, resp) - a signature was sent but is invalid, stale or replayed:
                      the caller refuses the request with resp.
    """
    if not request.headers.get(SIGNATURE_HEADER):
        return False, None
    if not _secret():
        log.warning("[BOT-INTERNAL] signed request received but BOT_INTERNAL_SECRET is unset; "
                    "treating it as unsigned.")
        return False, None
    ok, error = verify_current_request()
    return (True, None) if ok else (False, error)


def require_bot_signature(fn):
    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        ok, error = verify_current_request()
        if not ok:
            return error
        return fn(*args, **kwargs)
    return wrapper
