"""
Address verification rules (V5, Phase 4). Deterministic; no AI.

- Off unless ADDRESS_VERIFICATION_ENABLED is true. When off, nothing can be
  started and no "Address verified" claim is shown.
- Optional for sellers. Included in the existing Verified Pro plan (an active
  paid plan is required, as for the identity check); there is no separate
  price, and nothing else buys it.
- The result is a separate Passport claim, "address_verified", present only
  when the latest check is VERIFIED, from a real provider, and still within
  ADDRESS_VERIFICATION_VALIDITY_DAYS (default 365). It never feeds the Trust
  Score (D3).
- Siiqo stores only the outcome, its dates and the provider reference. The
  address and NIN go to the provider and are not saved.
"""
from __future__ import annotations

import logging
import os
import re
from datetime import datetime, timedelta, timezone

from app.extensions import db
from app.models.address_verification import AddressStatus, AddressVerification

from . import get_provider
from .base import AddressCheck, AddressInput, AddressRejected, ProviderUnavailable

logger = logging.getLogger(__name__)

CONSENT_VERSION = "2026-10"
REFRESH_EVERY = timedelta(minutes=10)
ATTEMPT_WINDOW = timedelta(days=30)

# (field, required, max length): the request body keys the website sends.
ADDRESS_FIELDS = (
    ("building_number", True, 20),
    ("street", True, 120),
    ("landmark", True, 120),
    ("city", True, 60),
    ("state", True, 40),
    ("lga", False, 60),
    ("flat_number", False, 20),
    ("building_name", False, 120),
    ("sub_street", False, 120),
)


class AddressVerificationError(Exception):
    """A rule was not met. `code` is stable for the frontend; `status` is the HTTP code."""

    def __init__(self, code: str, message: str, status: int = 400):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status


def _now():
    return datetime.now(timezone.utc)


def _aware(dt):
    return dt.replace(tzinfo=timezone.utc) if dt is not None and dt.tzinfo is None else dt


def _env_int(name, default, minimum=1):
    try:
        return max(minimum, int(os.environ.get(name) or default))
    except ValueError:
        return default


def is_enabled() -> bool:
    return os.environ.get("ADDRESS_VERIFICATION_ENABLED", "").strip().lower() in ("1", "true", "yes")


def validity_days() -> int:
    return _env_int("ADDRESS_VERIFICATION_VALIDITY_DAYS", 365)


def max_attempts() -> int:
    """Checks a seller may start per 30 days (each one is a paid agent visit)."""
    return _env_int("ADDRESS_VERIFICATION_MAX_ATTEMPTS", 3)


def eligible(user) -> bool:
    """Included in Verified Pro: an active paid plan is required."""
    from app.services.entitlements import plan_active
    return plan_active(user.id)


def latest(user_id) -> AddressVerification | None:
    return (AddressVerification.query.filter_by(user_id=user_id)
            .order_by(AddressVerification.id.desc()).first())


def valid_until(row: AddressVerification | None):
    if row is None or row.status != AddressStatus.VERIFIED or row.completed_at is None:
        return None
    return _aware(row.completed_at) + timedelta(days=validity_days())


def is_current(row: AddressVerification | None, now=None) -> bool:
    until = valid_until(row)
    return bool(until and until > (now or _now()))


def has_public_claim(user_id, now=None) -> bool:
    """True only when the flag is on and the latest check is a current, real VERIFIED result."""
    if not is_enabled():
        return False
    row = latest(user_id)
    return bool(row and row.provider != "mock" and is_current(row, now))


# ── starting a check ────────────────────────────────────────────────────────

def _clean_address(raw) -> AddressInput:
    if not isinstance(raw, dict):
        raise AddressVerificationError("ADDRESS_REQUIRED", "Enter your business address.")
    values = {}
    for field, required, max_len in ADDRESS_FIELDS:
        value = raw.get(field)
        value = re.sub(r"\s+", " ", value).strip() if isinstance(value, str) else ""
        if required and not value:
            raise AddressVerificationError("ADDRESS_INCOMPLETE", f"Enter the {field.replace('_', ' ')}.")
        if len(value) > max_len:
            raise AddressVerificationError("ADDRESS_TOO_LONG", f"The {field.replace('_', ' ')} is too long.")
        values[field] = value or None
    return AddressInput(**values)


def start(user, *, nin: str, address, consent: bool) -> AddressVerification:
    if not is_enabled():
        raise AddressVerificationError("COMING_SOON", "Address verification is coming soon.", 404)
    if not eligible(user):
        raise AddressVerificationError("PLAN_REQUIRED",
                                       "Address verification is included in Verified Pro.", 402)
    if consent is not True:
        raise AddressVerificationError("CONSENT_REQUIRED",
                                       "Please agree to the address check before continuing.")
    digits = re.sub(r"\D", "", nin or "")
    if len(digits) != 11:
        raise AddressVerificationError("INVALID_NIN", "Enter your 11-digit NIN.")
    addr = _clean_address(address)

    now = _now()
    current = latest(user.id)
    if current and current.status == AddressStatus.PENDING:
        raise AddressVerificationError("ALREADY_PENDING", "Your address check is already in progress.", 409)
    recent = AddressVerification.query.filter(AddressVerification.user_id == user.id,
                                              AddressVerification.requested_at >= now - ATTEMPT_WINDOW).count()
    if recent >= max_attempts():
        raise AddressVerificationError("TOO_MANY_ATTEMPTS",
                                       "You've reached the limit of address checks for now. "
                                       "Please try again later or contact support@siiqo.com.", 429)

    provider = get_provider()
    try:
        check = provider.request_check(user_id=user.id, nin=digits, address=addr)
    except AddressRejected:
        raise AddressVerificationError("DETAILS_REJECTED",
                                       "We couldn't start the check with these details. "
                                       "Check your NIN and address, then try again.")
    except ProviderUnavailable as e:
        logger.warning("[ADDRESS] provider unavailable for user %s: %s", user.id, e)
        raise AddressVerificationError("PROVIDER_UNAVAILABLE",
                                       "Address checks are unavailable right now. Please try again later.", 503)

    row = AddressVerification(user_id=user.id, provider=provider.name, provider_ref=check.provider_ref or None,
                              status=AddressStatus.PENDING, consent_version=CONSENT_VERSION, consent_at=now,
                              requested_at=now, last_checked_at=now)
    db.session.add(row)
    db.session.flush()
    _apply(row, check, user)
    db.session.commit()
    return row


# ── results ─────────────────────────────────────────────────────────────────

_NOTICE_KEYS = {
    AddressStatus.VERIFIED: "address.verified",
    AddressStatus.NOT_VERIFIED: "address.not_verified",
    AddressStatus.UNABLE_TO_VERIFY: "address.unable",
}


def _notify(user, status):
    key = _NOTICE_KEYS.get(status)
    if not key or user is None:
        return
    from app.i18n import message, user_locale
    from app.models.communication import Notification
    locale = user_locale(user)
    title, body = message(f"{key}.title", locale), message(f"{key}.body", locale)
    db.session.add(Notification(user_id=user.id, title=title, message=body, type="ACCOUNT"))
    if user.email:
        try:
            from app.utils.email import send_siiqo_email
            send_siiqo_email(to_email=user.email, subject=title, template_name="system_notice", locale=locale,
                             first_name=user.first_name or "there", notice_text=body)
        except Exception as e:  # email must never undo the result
            logger.warning("[ADDRESS] notice email failed for user %s: %s", user.id, type(e).__name__)


def _apply(row: AddressVerification, check: AddressCheck | None, user=None) -> AddressVerification:
    row.last_checked_at = _now()
    if check is None or row.status in AddressStatus.FINAL:
        return row
    if check.provider_ref and not row.provider_ref:
        row.provider_ref = check.provider_ref
    if check.outcome == AddressStatus.PENDING or check.outcome not in AddressStatus.FINAL:
        return row
    row.status = check.outcome
    row.reason_code = check.reason_code
    row.completed_at = _now()
    if user is None:
        from app.models.user import User
        user = db.session.get(User, row.user_id)
    _notify(user, row.status)
    return row


def refresh(row: AddressVerification, *, force: bool = False) -> AddressVerification:
    """Ask the provider for news on a pending check (at most every 10 minutes)."""
    if row is None or row.status != AddressStatus.PENDING or not row.provider_ref:
        return row
    last = _aware(row.last_checked_at)
    if not force and last and _now() - last < REFRESH_EVERY:
        return row
    if row.provider != get_provider().name:
        return row  # provider was switched; this check can't be read any more
    _apply(row, get_provider().fetch_check(row.provider_ref))
    db.session.commit()
    return row


def refresh_pending(batch: int = 100) -> int:
    """For a scheduled job: refresh pending checks. Returns how many changed."""
    if not is_enabled():
        return 0
    changed = 0
    for row in (AddressVerification.query.filter_by(status=AddressStatus.PENDING)
                .order_by(AddressVerification.id.asc()).limit(batch).all()):
        before = row.status
        refresh(row)
        changed += row.status != before
    return changed


# ── views ───────────────────────────────────────────────────────────────────

def _iso(dt):
    return _aware(dt).isoformat() if dt else None


def seller_summary(user, now=None) -> dict:
    """What the Trust tab shows. Never contains the address or the NIN."""
    now = now or _now()
    enabled = is_enabled()
    row = latest(user.id) if enabled else None
    if row is not None and row.status == AddressStatus.PENDING:
        refresh(row)
    current = is_current(row, now)
    status = row.status if row else "NOT_STARTED"
    if row and row.status == AddressStatus.VERIFIED and not current:
        status = "EXPIRED"
    return {
        "available": enabled,
        "coming_soon": not enabled,
        "eligible": bool(enabled and eligible(user)),
        "status": status,
        "address_verified": current,
        "shown_on_passport": bool(current and row.provider != "mock"),
        "is_test_result": bool(row and row.provider == "mock"),
        "reason_code": row.reason_code if row else None,
        "requested_at": _iso(row.requested_at) if row else None,
        "completed_at": _iso(row.completed_at) if row else None,
        "valid_until": _iso(valid_until(row)),
        "can_start": bool(enabled and (row is None or row.status != AddressStatus.PENDING)),
        "affects_trust_score": False,
    }


def export_for(user) -> list[dict]:
    """Privacy export: outcome and dates only (no address; the provider reference is
    kept out, like the identity check's)."""
    rows = (AddressVerification.query.filter_by(user_id=user.id)
            .order_by(AddressVerification.id.asc()).all())
    return [{
        "id": r.id,
        "provider": r.provider,
        "status": r.status,
        "reason_code": r.reason_code,
        "consent_version": r.consent_version,
        "consent_at": _iso(r.consent_at),
        "requested_at": _iso(r.requested_at),
        "completed_at": _iso(r.completed_at),
        "valid_until": _iso(valid_until(r)),
    } for r in rows]
