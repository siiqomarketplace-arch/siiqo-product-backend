"""
Verification lifecycle: expiry reminders and risk re-checks (Phase 2, item 2-3).

Expiry reminders
    30, 7 and 1 days before `VerificationStatus.valid_until`, plus one notice
    when the check has ended. Each goes once per check: a row in
    `verification_reminders` keyed by (user, valid_until, kind). When the check
    ends, `VerificationStatus.public_flags()` stops reporting the mark (it is
    computed from `valid_until` at read time), so the mark lapses on its own.

Risk re-checks
    A small, explicit set of events marks a verified seller as needing a new
    identity check (RISK_EVENTS). The seller keeps the Verified mark for a
    grace period (VERIFICATION_RECHECK_GRACE_DAYS, default 7); after that
    `public_flags()` pauses the mark until a new check passes. There is no
    manual review queue (V9): passing a new check clears the re-check.

Everything is written to `verification_audit_events`; admin requests are also
written to `admin_audit_logs`. Nothing here calls a provider, gateway or the
network except the seller notice email (which fails safe without mail keys).
"""
from __future__ import annotations

import logging
import os
import re
from datetime import datetime, timedelta, timezone
from html import escape

from app.extensions import db
from app.models.verification import (VerificationAuditEvent, VerificationReminder,
                                     VerificationStatus, VerificationWatch)

logger = logging.getLogger(__name__)

# ── configuration ───────────────────────────────────────────────────────────

# (kind, days before valid_until). Each reminder fires once, on the first daily
# run inside its window; only the most urgent due reminder is sent.
REMINDERS = (("D30", 30), ("D7", 7), ("D1", 1))
# The "your check has ended" notice is only sent this long after expiry, so
# old expirations are not notified when this ships.
EXPIRED_NOTICE_WINDOW_DAYS = 7
DISPUTE_WINDOW_DAYS = 30

PAYOUT_NAME_CHANGED = "PAYOUT_NAME_CHANGED"
STORE_NAME_CHANGED = "STORE_NAME_CHANGED"
SELLER_FAULT_DISPUTES = "SELLER_FAULT_DISPUTES"
ADMIN_REQUEST = "ADMIN_REQUEST"
# A new check that ended in a fraud signal or a face mismatch pauses the mark
# at once (no grace). Fraud is final for self-service (V2: "mark never shown");
# a face mismatch may be retried.
IDENTITY_NOT_CONFIRMED = "IDENTITY_NOT_CONFIRMED"
FACE_NOT_CONFIRMED = "FACE_NOT_CONFIRMED"
FINAL_REASONS = {IDENTITY_NOT_CONFIRMED}

# Seller-facing reasons. Fixed text only (never admin notes or user input).
RISK_EVENTS = {
    PAYOUT_NAME_CHANGED: "Your payout bank account is now in a different name from the one on your last check.",
    STORE_NAME_CHANGED: "Your store name changed after your last check.",
    SELLER_FAULT_DISPUTES: "Several recent orders were refunded to buyers after disputes.",
    ADMIN_REQUEST: "Siiqo has asked for a new identity check on your account.",
    IDENTITY_NOT_CONFIRMED: "We couldn't confirm your identity on your latest check.",
    FACE_NOT_CONFIRMED: "We couldn't confirm your identity on your latest check.",
}


def _now():
    return datetime.now(timezone.utc)


def _aware(dt):
    if dt is not None and dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


def _int_env(name, default, minimum=1):
    try:
        return max(minimum, int(os.environ.get(name, str(default))))
    except ValueError:
        return default


def grace_days() -> int:
    return _int_env("VERIFICATION_RECHECK_GRACE_DAYS", 7, minimum=0)


def dispute_threshold() -> int:
    return _int_env("VERIFICATION_RECHECK_DISPUTES", 3)


def reverify_url() -> str:
    site = os.environ.get("SITE_URL", "https://siiqo.com").rstrip("/")
    return f"{site}/vendor/trust?reverify=1"


def reason_text(code) -> str | None:
    return RISK_EVENTS.get(code) if code else None


def _fmt(dt) -> str:
    return f"{_aware(dt):%d %b %Y}"


# ── audit and notices ───────────────────────────────────────────────────────

def _audit(user_id, event, reason_code=None, actor_type="SYSTEM", actor_id=None, details=None):
    db.session.add(VerificationAuditEvent(user_id=user_id, event=event, reason_code=reason_code,
                                          actor_type=actor_type, actor_id=actor_id, details=details))


def _notify(user_id, title, message, email_subject, with_link=True):
    """In-app notification plus email, both with the one-tap re-verify link
    (unless with_link is False). Email failure never blocks the notice."""
    from app.models.communication import Notification
    from app.models.user import User

    link = reverify_url() if with_link else None
    db.session.add(Notification(user_id=user_id, title=title, type="VERIFICATION",
                                message=f"{message} Re-verify: {link}" if link else message))
    user = db.session.get(User, user_id)
    if not user or not user.email:
        return
    try:
        from app.utils.email import send_siiqo_email
        send_siiqo_email(to_email=user.email, subject=email_subject, template_name="system_notice",
                         first_name=user.first_name or "there", notice_text=escape(message),
                         cta_url=link, cta_label="Re-verify now" if link else None)
    except Exception as exc:  # noqa: BLE001 — a mail problem must not stop the job
        logger.warning("[VERIFICATION] notice email failed for user %s: %s", user_id, exc)


# ── baseline (what risk events compare against) ────────────────────────────

def _norm_store_name(name) -> str:
    return re.sub(r"\s+", " ", (name or "").strip().lower())


def _current_store_name(user_id):
    from app.models.user import Storefront
    sf = Storefront.query.filter_by(vendor_id=user_id).first()
    return sf.store_name if sf else None


def _current_payout_name(user_id):
    from app.services.verification.service import _payout_account_name
    return _payout_account_name(user_id)


def _verified_name(user_id):
    from app.models.verification import CaseStatus, VerificationCase, VerificationKind
    case = (VerificationCase.query
            .filter_by(user_id=user_id, kind=VerificationKind.IDENTITY, status=CaseStatus.PASSED)
            .order_by(VerificationCase.completed_at.desc()).first())
    return case.verified_name if case else None


def _watch(user_id, create=True):
    watch = VerificationWatch.query.filter_by(user_id=user_id).first()
    if watch is None and create:
        watch = VerificationWatch(user_id=user_id)
        db.session.add(watch)
    return watch


def _set_baseline(watch, user_id, now=None):
    watch.baseline_store_name = _current_store_name(user_id)
    watch.baseline_payout_name = _current_payout_name(user_id)
    watch.baseline_at = now or _now()


def on_identity_passed(user_id, case_id=None):
    """Called when an identity check passes (inside its transaction): resets the
    baseline and clears any open re-check. The caller commits."""
    now = _now()
    watch = _watch(user_id)
    if watch.recheck_open:
        _audit(user_id, "RECHECK_CLEARED", watch.recheck_reason_code, actor_type="SELLER",
               actor_id=user_id, details={"case_id": case_id})
    watch.recheck_reason_code = None
    watch.recheck_source = None
    watch.recheck_requested_at = None
    watch.recheck_grace_until = None
    watch.recheck_paused_at = None
    _set_baseline(watch, user_id, now)


# ── re-check requests ───────────────────────────────────────────────────────

def _identity_current(user_id, now=None):
    status = VerificationStatus.query.filter_by(user_id=user_id).first()
    return bool(status and status.is_identity_current(now))


def request_recheck(user_id, reason_code, *, actor_type="SYSTEM", actor_id=None, details=None, now=None):
    """Open a re-check for a verified seller. Idempotent: if one is already open
    it is returned unchanged. Returns the watch row, or None when the seller has
    no current identity check (nothing to re-check). The caller commits."""
    if reason_code not in RISK_EVENTS:
        raise ValueError(f"Unknown re-check reason: {reason_code}")
    now = now or _now()
    if not _identity_current(user_id, now):
        return None
    watch = _watch(user_id)
    if watch.recheck_open:
        return watch
    if watch.baseline_at is None:
        _set_baseline(watch, user_id, now)
    grace_until = now + timedelta(days=grace_days())
    watch.recheck_reason_code = reason_code
    watch.recheck_source = "ADMIN" if actor_type == "ADMIN" else "SYSTEM"
    watch.recheck_requested_at = now
    watch.recheck_grace_until = grace_until
    watch.recheck_paused_at = None
    _audit(user_id, "RECHECK_REQUESTED", reason_code, actor_type=actor_type, actor_id=actor_id,
           details={**(details or {}), "grace_until": grace_until.isoformat()})
    _notify(user_id, "Re-check needed to keep your Verified mark",
            f"{RISK_EVENTS[reason_code]} To keep your Verified mark, please re-verify by {_fmt(grace_until)}. "
            "It takes about a minute with your NIN and a live selfie. If you don't, your Verified mark "
            "pauses until you re-verify. Your store, products and orders are not affected.",
            "Re-check needed to keep your Verified mark | Siiqo")
    logger.info("[VERIFICATION] re-check %s opened for user %s", reason_code, user_id)
    return watch


def pause_after_failed_check(user_id, case, now=None):
    """A new identity check by a seller whose mark is still valid ended in a
    fraud signal (FAILED_FRAUD) or a face mismatch: pause the mark now, with no
    grace period. Technical failures (provider error, timeout, unreadable
    selfie) never come here, so the current mark stays and the seller retries.
    Returns True when the mark was paused. The caller commits."""
    from app.models.verification import CaseStatus
    now = now or _now()
    if not _identity_current(user_id, now):
        return False  # no current mark to pause
    fraud = case.status == CaseStatus.FAILED_FRAUD
    reason = IDENTITY_NOT_CONFIRMED if fraud else FACE_NOT_CONFIRMED
    watch = _watch(user_id)
    if watch.baseline_at is None:
        _set_baseline(watch, user_id, now)
    if watch.recheck_reason_code in FINAL_REASONS:
        return False  # already paused for fraud
    previous = watch.recheck_reason_code
    watch.recheck_reason_code = reason
    watch.recheck_source = "SYSTEM"
    watch.recheck_requested_at = now
    watch.recheck_grace_until = now
    watch.recheck_paused_at = now
    details = {"case_id": case.id, "provider_reason": case.reason_code}
    if previous:
        details["replaces"] = previous
    _audit(user_id, "FRAUD_RESULT" if fraud else "FACE_MISMATCH_RESULT", reason, details=details)
    _audit(user_id, "MARK_PAUSED", reason, details={"case_id": case.id, "grace": False})
    if fraud:
        _notify(user_id, "We couldn't confirm your identity",
                "We couldn't confirm your identity on your latest check, so your Verified mark is paused. "
                "Please contact support@siiqo.com. Your store, products and orders are not affected.",
                "We couldn't confirm your identity | Siiqo", with_link=False)
    else:
        _notify(user_id, "We couldn't confirm your identity",
                "We couldn't confirm your identity on your latest check, so your Verified mark is paused. "
                "You can try again from your Trust tab, or contact support@siiqo.com. "
                "Your store, products and orders are not affected.",
                "We couldn't confirm your identity | Siiqo")
    logger.warning("[VERIFICATION] mark paused for user %s after %s on case %s", user_id, reason, case.id)
    return True


def _seller_fault_disputes(user_id, since) -> int:
    """Read-only count of disputes a reviewer decided were the seller's fault
    since `since` (dispute cases, app/models/dispute.py). Older disputes that
    predate dispute cases count when they ended refunded to the buyer."""
    from app.models.dispute import DisputeCase
    from app.models.escrow import EscrowStatus, EscrowTransaction
    from app.models.order import Order
    decided = (db.session.query(DisputeCase.id)
               .filter(DisputeCase.vendor_id == user_id,
                       DisputeCase.seller_fault.is_(True),
                       DisputeCase.decided_at >= since)
               .count())
    with_case = db.session.query(DisputeCase.order_id)
    legacy = (db.session.query(EscrowTransaction.id)
              .join(Order, Order.id == EscrowTransaction.order_id)
              .filter(Order.vendor_id == user_id,
                      EscrowTransaction.dispute_id.isnot(None),
                      EscrowTransaction.status == EscrowStatus.REFUNDED,
                      EscrowTransaction.updated_at >= since,
                      Order.id.notin_(with_case))
              .count())
    return decided + legacy


def evaluate_risk_events(user_id, now=None):
    """Compare the seller's current details with the baseline from their last
    check and open a re-check on the first risk event found. Returns the reason
    code opened, or None. The caller commits."""
    now = now or _now()
    if not _identity_current(user_id, now):
        return None
    watch = _watch(user_id)
    if watch.recheck_open:
        return None
    if watch.baseline_at is None:
        # Checks that passed before this existed: today's details become the baseline.
        _set_baseline(watch, user_id, now)
        return None

    from app.services.verification.matching import names_match

    payout = _current_payout_name(user_id)
    reference = watch.baseline_payout_name or _verified_name(user_id)
    if payout and reference and not names_match(payout, reference):
        request_recheck(user_id, PAYOUT_NAME_CHANGED, now=now)
        return PAYOUT_NAME_CHANGED

    store = _current_store_name(user_id)
    if watch.baseline_store_name and _norm_store_name(store) != _norm_store_name(watch.baseline_store_name):
        request_recheck(user_id, STORE_NAME_CHANGED, now=now)
        return STORE_NAME_CHANGED

    since = max(now - timedelta(days=DISPUTE_WINDOW_DAYS), _aware(watch.baseline_at))
    count = _seller_fault_disputes(user_id, since)
    if count >= dispute_threshold():
        request_recheck(user_id, SELLER_FAULT_DISPUTES, now=now,
                        details={"disputes": count, "window_days": DISPUTE_WINDOW_DAYS})
        return SELLER_FAULT_DISPUTES
    return None


def check_after_change(user_id):
    """Hook for routes that change a seller's payout account or store name.
    Runs after the route's own commit and never breaks the request."""
    try:
        evaluate_risk_events(int(user_id))
        db.session.commit()  # a new re-check, or a first baseline
    except Exception as exc:  # noqa: BLE001
        db.session.rollback()
        logger.warning("[VERIFICATION] risk check failed for user %s: %s", user_id, exc)


# ── daily upkeep ────────────────────────────────────────────────────────────

def _reminder_due(valid_until, now):
    """Which reminder (if any) is due now for a check ending at valid_until."""
    left = (valid_until - now).total_seconds() / 86400
    if left <= 0:
        return "EXPIRED" if -left <= EXPIRED_NOTICE_WINDOW_DAYS else None
    due = None
    for kind, days in REMINDERS:      # 30, 7, 1: keep the most urgent window we are in
        if left <= days:
            due = kind
    return due


def _record_reminder(user_id, cycle, kind) -> bool:
    """Insert the reminder row; False if it was already sent (also under a race)."""
    from sqlalchemy.exc import IntegrityError
    if VerificationReminder.query.filter_by(user_id=user_id, cycle=cycle, kind=kind).first():
        return False
    try:
        with db.session.begin_nested():
            db.session.add(VerificationReminder(user_id=user_id, cycle=cycle, kind=kind))
    except IntegrityError:
        return False
    return True


def send_expiry_reminders(now=None) -> int:
    now = now or _now()
    sent = 0
    for row in VerificationStatus.query.filter(VerificationStatus.identity_verified_at.isnot(None),
                                               VerificationStatus.valid_until.isnot(None)).all():
        valid_until = _aware(row.valid_until)
        kind = _reminder_due(valid_until, now)
        if not kind:
            continue
        cycle = valid_until.strftime("%Y-%m-%dT%H:%M:%S")
        if not _record_reminder(row.user_id, cycle, kind):
            continue
        date = _fmt(valid_until)
        if kind == "EXPIRED":
            _notify(row.user_id, "Your identity check has ended",
                    f"Your Siiqo identity check ended on {date}, so the Verified mark is no longer shown "
                    "on your store. Re-verify with your NIN and a live selfie to show it again. "
                    "Your store, products and orders are not affected.",
                    "Your identity check has ended | Siiqo")
        else:
            _notify(row.user_id, f"Re-verify by {date}",
                    f"Your Siiqo identity check is valid until {date}. To keep showing the Verified mark "
                    f"after that, re-verify by {date}. It takes about a minute with your NIN and a live selfie.",
                    f"Re-verify by {date} to keep your Verified mark | Siiqo")
        _audit(row.user_id, "REMINDER_SENT", details={"kind": kind, "valid_until": valid_until.isoformat()})
        sent += 1
    return sent


def record_pauses(now=None) -> int:
    """Log (once) and tell the seller when a re-check's grace period ends.
    The pause itself is applied at read time by public_flags()."""
    now = now or _now()
    paused = 0
    for watch in VerificationWatch.query.filter(VerificationWatch.recheck_reason_code.isnot(None),
                                                VerificationWatch.recheck_paused_at.is_(None)).all():
        if not watch.is_paused(now):
            continue
        watch.recheck_paused_at = now
        _audit(watch.user_id, "MARK_PAUSED", watch.recheck_reason_code)
        _notify(watch.user_id, "Your Verified mark is paused",
                "Your Verified mark is paused because the re-check wasn't completed in time. "
                "Re-verify with your NIN and a live selfie to show it again. "
                "Your store, products and orders are not affected.",
                "Your Verified mark is paused | Siiqo")
        paused += 1
    return paused


def run_verification_upkeep(now=None) -> dict:
    """Daily: expiry reminders, risk-event sweep (catches changes made outside
    the hooked routes) and pause notices. Called from run_daily_billing_tasks."""
    now = now or _now()
    reminders = send_expiry_reminders(now)
    db.session.commit()

    opened = 0
    for (user_id,) in db.session.query(VerificationStatus.user_id).all():
        try:
            if evaluate_risk_events(user_id, now):
                opened += 1
            db.session.commit()
        except Exception as exc:  # noqa: BLE001 — one seller's problem must not stop the rest
            db.session.rollback()
            logger.warning("[VERIFICATION] risk sweep failed for user %s: %s", user_id, exc)

    paused = record_pauses(now)
    db.session.commit()
    result = {"reminders": reminders, "rechecks_opened": opened, "paused": paused}
    logger.info("[VERIFICATION] daily upkeep: %s", result)
    return result


# ── seller read model ───────────────────────────────────────────────────────

def seller_state(user_id, status=None, now=None) -> dict:
    """What the Trust tab shows about expiry and re-checks."""
    now = now or _now()
    status = status or VerificationStatus.query.filter_by(user_id=user_id).first()
    watch = VerificationWatch.query.filter_by(user_id=user_id).first()
    current = bool(status and status.is_identity_current(now))
    valid_until = _aware(status.valid_until) if status and status.valid_until else None

    recheck = None
    if current and watch and watch.recheck_open:
        recheck = {
            "reason_code": watch.recheck_reason_code,
            "reason": reason_text(watch.recheck_reason_code),
            "requested_at": _aware(watch.recheck_requested_at).isoformat() if watch.recheck_requested_at else None,
            "reverify_by": _aware(watch.recheck_grace_until).isoformat() if watch.recheck_grace_until else None,
            "paused": watch.is_paused(now),
            "contact_support": watch.recheck_reason_code in FINAL_REASONS,
        }
    window = timedelta(days=REMINDERS[0][1])
    expiring = bool(current and valid_until and valid_until - now <= window)
    return {
        "recheck": recheck,
        "reverify_by": (recheck["reverify_by"] if recheck
                        else valid_until.isoformat() if expiring else None),
        "can_reverify": bool((recheck or expiring) and not (recheck and recheck["contact_support"])),
        "mark_paused": bool(recheck and recheck["paused"]),
    }


def reverify_allowed(user_id, now=None) -> bool:
    """A still-current seller may start a new check inside the reminder window
    or while a re-check is open."""
    return seller_state(user_id, now=now)["can_reverify"]
