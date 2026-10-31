"""
Verification v2 business rules (deterministic; no AI).

Flow: start identity (consent + NIN) -> provider hosted selfie -> result
(webhook or polling) -> PASS / retry / final fail / on hold -> optional
business (CAC) step for BUSINESS sellers.
"""
from __future__ import annotations

import hashlib
import hmac
import os
import re
import uuid
from datetime import datetime, timedelta, timezone

from flask import current_app

from app.extensions import db
from app.models.user import Storefront, User
from app.models.verification import (CaseStatus, VerificationCase, VerificationCheck,
                                     VerificationKind, VerificationStatus)
from app.models.withdrawal import VendorBankAccount

from . import get_provider
from .base import IdentityResult, Outcome
from .matching import names_match

CONSENT_VERSION = "2026-10"


class VerificationError(Exception):
    """A rule was not met. `code` is stable for the frontend; `status` is the HTTP code."""

    def __init__(self, code: str, message: str, status: int = 400):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status


def _utcnow():
    return datetime.now(timezone.utc)


def _aware(dt):
    if dt is not None and dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


# ── availability (feature flag) ─────────────────────────────────────────────

def _allowlist() -> set[int]:
    raw = os.environ.get("VERIFICATION_V2_ALLOWLIST", "")
    return {int(x) for x in re.findall(r"\d+", raw)}


def is_public() -> bool:
    return os.environ.get("VERIFICATION_V2_ENABLED", "").strip().lower() in ("1", "true", "yes")


def is_available(user: User) -> bool:
    """Public once launched; before that only allowlisted staff can use it."""
    return is_public() or (user is not None and user.id in _allowlist())


# ── helpers ─────────────────────────────────────────────────────────────────

def hash_id_number(value: str) -> str:
    """Keyed hash so duplicates can be detected without storing the number."""
    digits = re.sub(r"\D", "", value or "")
    key = (current_app.config.get("SECRET_KEY") or "").encode()
    return hmac.new(key, f"siiqo-id:{digits}".encode(), hashlib.sha256).hexdigest()


def validity_days() -> int:
    try:
        return max(1, int(os.environ.get("VERIFICATION_VALIDITY_DAYS", "365")))
    except ValueError:
        return 365


def max_attempts_per_week() -> int:
    try:
        return max(1, int(os.environ.get("VERIFICATION_MAX_ATTEMPTS_7D", "3")))
    except ValueError:
        return 3


def _status_row(user_id: int) -> VerificationStatus:
    row = VerificationStatus.query.filter_by(user_id=user_id).first()
    if not row:
        row = VerificationStatus(user_id=user_id)
        db.session.add(row)
    return row


def _payout_account_name(user_id: int) -> str | None:
    acc = (VendorBankAccount.query.filter_by(vendor_id=user_id, is_default=True).first()
           or VendorBankAccount.query.filter_by(vendor_id=user_id).first())
    if acc and acc.account_name:
        return acc.account_name
    sf = Storefront.query.filter_by(vendor_id=user_id).first()
    return sf.account_name if sf and sf.account_name else None


# ── identity ────────────────────────────────────────────────────────────────

def start_identity(user: User, *, seller_type: str, nin: str, consent: bool) -> VerificationCase:
    if not is_available(user):
        raise VerificationError("COMING_SOON", "Verification is coming soon.", 403)
    if not user.storefront:
        raise VerificationError("NO_STORE", "Create your store before verifying.", 400)
    if not consent:
        raise VerificationError("CONSENT_REQUIRED", "Please agree to the verification check.", 400)

    seller_type = (seller_type or "INDIVIDUAL").upper()
    if seller_type not in ("INDIVIDUAL", "BUSINESS"):
        raise VerificationError("INVALID_SELLER_TYPE", "Choose Individual or Business.", 400)

    nin_digits = re.sub(r"\D", "", nin or "")
    if len(nin_digits) != 11:
        raise VerificationError("INVALID_NIN", "Enter your 11-digit NIN.", 400)

    status = VerificationStatus.query.filter_by(user_id=user.id).first()
    if status and status.is_identity_current():
        # Re-verification opens 30 days before expiry, or while a re-check is open.
        from .rechecks import reverify_allowed, seller_state
        if not reverify_allowed(user.id):
            recheck = seller_state(user.id, status)["recheck"]
            if recheck and recheck["contact_support"]:
                raise VerificationError("CONTACT_SUPPORT",
                                        "We couldn't confirm your identity. Please contact support@siiqo.com.", 403)
            raise VerificationError("ALREADY_VERIFIED", "Your identity is already verified.", 409)

    # Resume an unfinished session instead of paying for a new provider call.
    open_case = (VerificationCase.query
                 .filter_by(user_id=user.id, kind=VerificationKind.IDENTITY,
                            status=CaseStatus.IN_PROGRESS)
                 .order_by(VerificationCase.created_at.desc()).first())
    if open_case:
        return open_case

    week_ago = _utcnow() - timedelta(days=7)
    recent = (VerificationCase.query
              .filter(VerificationCase.user_id == user.id,
                      VerificationCase.kind == VerificationKind.IDENTITY,
                      VerificationCase.created_at >= week_ago).count())
    if recent >= max_attempts_per_week():
        raise VerificationError("TOO_MANY_ATTEMPTS",
                                "You've reached the attempt limit. Please try again in a few days "
                                "or contact support.", 429)

    provider = get_provider()
    # Pay first: a real provider check is part of Verified Pro. Mock checks are
    # staff testing and need no plan.
    if provider.name != "mock":
        from app.services.entitlements import plan_active
        if not plan_active(user.id):
            raise VerificationError("PLAN_REQUIRED", "Get Verified Pro to verify your identity.", 402)
    id_hash = hash_id_number(nin_digits)
    case = VerificationCase(
        user_id=user.id,
        kind=VerificationKind.IDENTITY,
        seller_type=seller_type,
        provider=provider.name,
        status=CaseStatus.IN_PROGRESS,
        idempotency_key=f"ver-{user.id}-{uuid.uuid4().hex}",
        id_number_hash=id_hash,
        consent_version=CONSENT_VERSION,
        consent_at=_utcnow(),
    )
    db.session.add(case)
    db.session.flush()

    # Same identity already verified on another seller account: hold, don't call the provider.
    duplicate = (VerificationCase.query
                 .filter(VerificationCase.id_number_hash == id_hash,
                         VerificationCase.user_id != user.id,
                         VerificationCase.status == CaseStatus.PASSED).first())
    if duplicate:
        case.status = CaseStatus.ON_HOLD
        case.reason_code = "DUPLICATE_IDENTITY"
        case.completed_at = _utcnow()
        db.session.commit()
        return case

    site = os.environ.get("SITE_URL", "https://siiqo.com").rstrip("/")
    session = provider.start_identity(case_id=case.id, id_number=nin_digits,
                                      seller_name=user.full_name,
                                      return_url=f"{site}/vendor/trust?case={case.id}")
    case.provider_ref = session.provider_ref
    case.hosted_url = session.hosted_url
    db.session.commit()
    return case


def apply_identity_result(case: VerificationCase, result: IdentityResult) -> VerificationCase:
    """Record a provider result. Idempotent: final cases are never changed."""
    if case.status in CaseStatus.FINAL:
        return case

    for c in result.checks:
        db.session.add(VerificationCheck(case_id=case.id, check_type=c.check_type, result=c.result,
                                         score=c.score, threshold=c.threshold,
                                         detail_code=c.detail_code))
    case.reason_code = result.reason_code
    case.completed_at = _utcnow()

    if result.outcome == Outcome.RETRY:
        case.status = CaseStatus.FAILED_RETRYABLE
    elif result.outcome == Outcome.MISMATCH:
        case.status = CaseStatus.FAILED
    elif result.outcome == Outcome.FRAUD:
        case.status = CaseStatus.FAILED_FRAUD
    elif result.outcome == Outcome.INCONCLUSIVE:
        case.status = CaseStatus.ON_HOLD
    elif result.outcome == Outcome.PASS:
        case.verified_name = result.full_name
        payout_name = _payout_account_name(case.user_id)
        if payout_name and result.full_name and not names_match(result.full_name, payout_name):
            # V9: no review queue — seller updates their payout account or contacts support.
            case.status = CaseStatus.ON_HOLD
            case.reason_code = "PAYOUT_NAME_MISMATCH"
        else:
            case.status = CaseStatus.PASSED
            row = _status_row(case.user_id)
            now = _utcnow()
            row.seller_type = case.seller_type
            row.identity_verified_at = now
            row.valid_until = now + timedelta(days=validity_days())
            row.source_provider = case.provider
            row.business_verified_at = None  # business step must be redone with the new identity
            # New baseline for risk events; clears any open re-check (2-3).
            from .rechecks import on_identity_passed
            on_identity_passed(case.user_id, case.id)
            if case.provider == "mock":
                # Staff testing only: stands in for the paid subscription (VER-2).
                row.subscription_active = True
            else:
                # The Verified mark shows while the Verified Pro plan is active.
                from app.services.billing import sync_verification_flag
                db.session.flush()
                sync_verification_flag(case.user_id)
    else:
        case.status = CaseStatus.ON_HOLD
        case.reason_code = case.reason_code or "UNKNOWN_OUTCOME"

    # 2-3: a fraud signal or face mismatch on a new check pauses a still-valid
    # mark at once. Technical failures (RETRY, INCONCLUSIVE, provider errors)
    # keep the current mark so the seller can retry.
    if case.status == CaseStatus.FAILED_FRAUD or (
            case.status == CaseStatus.FAILED and case.reason_code == "FACE_MISMATCH"):
        from .rechecks import pause_after_failed_check
        pause_after_failed_check(case.user_id, case)

    db.session.commit()
    if case.status == CaseStatus.FAILED and case.provider != "mock":
        # Details didn't match: after the free retries, refund and end the plan.
        # Fraud signals (FAILED_FRAUD) are never refunded.
        from app.services.billing import handle_identity_mismatch
        handle_identity_mismatch(case.user_id)
    # Trust Platform webhooks (business.verified / verification_lapsed); never raises.
    from app.services.platform_webhooks import notify_passport_change
    notify_passport_change(case.user_id, hint="verification")
    return case


def submit_identity_selfie(user: User, case_id: int, *, nin: str, selfie_image: str) -> VerificationCase:
    """Providers without a hosted page (Youverify): the seller's selfie and NIN
    are checked together. The NIN must match the one the case was started with;
    only its hash is stored."""
    from .youverify_provider import ProviderUnavailable

    case = db.session.get(VerificationCase, case_id)
    if not case or case.user_id != user.id or case.kind != VerificationKind.IDENTITY:
        raise VerificationError("NOT_FOUND", "Verification not found.", 404)
    if case.status != CaseStatus.IN_PROGRESS:
        raise VerificationError("NOT_IN_PROGRESS", "This verification is already finished.", 409)
    nin_digits = re.sub(r"\D", "", nin or "")
    if len(nin_digits) != 11 or hash_id_number(nin_digits) != case.id_number_hash:
        raise VerificationError("NIN_CHANGED", "Enter the same NIN you started with.", 400)
    if not selfie_image or len(selfie_image) < 1000:
        raise VerificationError("NO_SELFIE", "Take a clear selfie to continue.", 400)

    provider = get_provider()
    if not hasattr(provider, "submit_identity"):
        raise VerificationError("NOT_SUPPORTED", "This verification step isn't used here.", 400)
    try:
        result = provider.submit_identity(case_id=case.id, id_number=nin_digits, selfie_image=selfie_image)
    except ProviderUnavailable as exc:
        import logging
        logging.getLogger(__name__).warning("[VERIFICATION] provider unavailable for case %s: %s", case.id, exc)
        raise VerificationError("PROVIDER_UNAVAILABLE",
                                "Verification is busy right now. Please try again in a few minutes.", 503)
    if result is None:
        case.status = CaseStatus.PENDING_PROVIDER  # the result will arrive by webhook
        db.session.commit()
        return case
    if result.provider_ref:
        case.provider_ref = result.provider_ref
    return apply_identity_result(case, result)


def refresh_identity_case(case: VerificationCase) -> VerificationCase:
    """Polling fallback for when a webhook is late or missing."""
    if case.status != CaseStatus.IN_PROGRESS or not case.provider_ref:
        return case
    provider = get_provider()
    user = db.session.get(User, case.user_id)
    result = provider.fetch_identity_result(case.provider_ref,
                                            seller_name=user.full_name if user else None)
    return apply_identity_result(case, result) if result else case


# ── business (CAC) ──────────────────────────────────────────────────────────

def verify_business(user: User, *, reg_number: str, business_type: str) -> VerificationCase:
    if not is_available(user):
        raise VerificationError("COMING_SOON", "Verification is coming soon.", 403)

    status = VerificationStatus.query.filter_by(user_id=user.id).first()
    if not status or not status.is_identity_current():
        raise VerificationError("IDENTITY_FIRST", "Verify your identity first.", 400)

    reg = re.sub(r"[^A-Za-z0-9]", "", reg_number or "").upper()
    if len(reg) < 4:
        raise VerificationError("INVALID_REG_NUMBER", "Enter your CAC registration number.", 400)
    business_type = (business_type or "COMPANY").upper()
    if business_type not in ("COMPANY", "BUSINESS_NAME", "INCORPORATED_TRUSTEES"):
        raise VerificationError("INVALID_BUSINESS_TYPE", "Choose your business type.", 400)

    identity_case = (VerificationCase.query
                     .filter_by(user_id=user.id, kind=VerificationKind.IDENTITY,
                                status=CaseStatus.PASSED)
                     .order_by(VerificationCase.completed_at.desc()).first())
    verified_name = identity_case.verified_name if identity_case else user.full_name

    provider = get_provider()
    case = VerificationCase(
        user_id=user.id, kind=VerificationKind.BUSINESS, seller_type="BUSINESS",
        provider=provider.name, status=CaseStatus.PENDING_PROVIDER,
        idempotency_key=f"verb-{user.id}-{uuid.uuid4().hex}",
        id_number_hash=hash_id_number(reg), consent_version=CONSENT_VERSION, consent_at=_utcnow(),
    )
    db.session.add(case)
    db.session.flush()

    result = provider.lookup_business(reg_number=reg, business_type=business_type)
    people = [verified_name if p == "__SELLER__" else p for p in result.people]
    is_director = any(names_match(verified_name or "", p) for p in people)

    db.session.add(VerificationCheck(case_id=case.id, check_type="cac_lookup",
                                     result="PASS" if result.outcome == Outcome.PASS else "FAIL",
                                     detail_code=result.reason_code))
    case.business_name = result.business_name
    case.completed_at = _utcnow()

    if result.outcome != Outcome.PASS:
        case.status, case.reason_code = CaseStatus.FAILED, result.reason_code or "BUSINESS_NOT_FOUND"
    elif not result.is_active:
        case.status, case.reason_code = CaseStatus.FAILED, "BUSINESS_INACTIVE"
    elif not is_director:
        db.session.add(VerificationCheck(case_id=case.id, check_type="director_match", result="FAIL"))
        case.status, case.reason_code = CaseStatus.ON_HOLD, "NOT_A_DIRECTOR"
    else:
        db.session.add(VerificationCheck(case_id=case.id, check_type="director_match", result="PASS"))
        case.status = CaseStatus.PASSED
        status.business_verified_at = _utcnow()
        status.seller_type = "BUSINESS"

    db.session.commit()
    return case


# ── read model ──────────────────────────────────────────────────────────────

def seller_summary(user: User) -> dict:
    status = VerificationStatus.query.filter_by(user_id=user.id).first()
    latest = (VerificationCase.query.filter_by(user_id=user.id)
              .order_by(VerificationCase.created_at.desc()).limit(5).all())
    # V7: sellers approved under the old manual review keep their badge until it expires.
    sf = user.storefront
    legacy_expiry = _aware(sf.pro_verified_expires_at) if sf else None
    legacy_active = bool(sf and sf.is_pro_verified
                         and (legacy_expiry is None or legacy_expiry > _utcnow()))
    from .rechecks import seller_state
    return {
        # Expiry and re-check state for the Trust tab (2-3): recheck, reverify_by,
        # can_reverify, mark_paused.
        **seller_state(user.id, status),
        "legacy": {
            "verified": legacy_active,
            "expires_at": legacy_expiry.isoformat() if legacy_active and legacy_expiry else None,
        },
        "available": is_available(user),
        "coming_soon": not is_available(user),
        "price_ngn_per_month": 1999,
        "identity_verified": bool(status and status.is_identity_current()),
        "business_verified": bool(status and status.business_verified_at
                                  and status.is_identity_current()),
        "seller_type": status.seller_type if status else None,
        "valid_until": _aware(status.valid_until).isoformat() if status and status.valid_until else None,
        "is_test_result": bool(status and status.source_provider == "mock"),
        "cases": [c.to_seller_dict() for c in latest],
    }
