"""
Verified Pro billing (docs/VERIFIED_PRO_PLAN.md).

Billing only decides whether a seller's plan is active; features come from
app/services/entitlements.py and identity from verification v2.

- Every subscription payment is recorded once by its Paystack reference, so a
  renewal reported by both `charge.success` and `invoice.payment_success`
  extends the plan once.
- A plan stays usable for GRACE_DAYS after a missed renewal (see entitlements).
- Identity mismatches: after MISMATCH_LIMIT failed checks in the paid period
  the latest payment is refunded and the plan ends. Fraud signals: no refund.
"""
import logging
import os
from datetime import timedelta

import requests
from dateutil.relativedelta import relativedelta

from app.extensions import db

PLAN_PRICES_NGN = {"PRO_MONTHLY": 1999, "PRO_ANNUAL": 19990}
MISMATCH_LIMIT = 3          # the first check plus 2 free retries
REMINDER_DAYS_BEFORE = 3
PAYSTACK_BASE_URL = "https://api.paystack.co"

logger = logging.getLogger(__name__)


def _now():
    from app.services.entitlements import _now as now
    return now()


def _aware(dt):
    from app.services.entitlements import _aware as aware
    return aware(dt)


def _paystack_key():
    return os.environ.get("PAYSTACK_SECRET_KEY", "")


def _interval(plan_name):
    return relativedelta(years=1) if "ANNUAL" in (plan_name or "") else relativedelta(months=1)


def _get_plan(plan_name):
    from app.models.admin import SubscriptionPlan
    plan = SubscriptionPlan.query.filter_by(name=plan_name).first()
    if not plan:
        plan = SubscriptionPlan(name=plan_name, price_ngn=PLAN_PRICES_NGN.get(plan_name, 1999),
                                features={"verified_pro": True}, is_active=True)
        db.session.add(plan)
        db.session.flush()
    return plan


# ── payments ──────────────────────────────────────────────────────────────

def apply_subscription_payment(user, plan_name, reference, amount_ngn=None):
    """Record a successful subscription payment and extend (or start) the plan.
    Idempotent per payment reference. Returns the subscription."""
    from app.models.admin import VendorSubscription
    from app.models.billing import SubscriptionPayment

    if reference:
        existing = SubscriptionPayment.query.filter_by(reference=reference).first()
        if existing:
            return db.session.get(VendorSubscription, existing.subscription_id) if existing.subscription_id else None

    now = _now()
    current = (VendorSubscription.query
               .filter(VendorSubscription.vendor_id == user.id,
                       VendorSubscription.status.in_(("ACTIVE", "CANCELLED_PENDING_EXPIRY")))
               .order_by(VendorSubscription.end_date.desc())
               .first())
    plan = _get_plan(plan_name)

    if current and current.plan_id == plan.id and current.end_date:
        # Renewal of the same plan: extend from the later of now and the end date.
        base = max(_aware(current.end_date), now)
        current.end_date = base + _interval(plan_name)
        current.status = "ACTIVE"
        sub = current
    else:
        for old in VendorSubscription.query.filter(
                VendorSubscription.vendor_id == user.id,
                VendorSubscription.status.in_(("ACTIVE", "CANCELLED_PENDING_EXPIRY"))).all():
            old.status = "SUPERSEDED"
        sub = VendorSubscription(vendor_id=user.id, plan_id=plan.id, status="ACTIVE",
                                 start_date=now, end_date=now + _interval(plan_name))
        db.session.add(sub)
    db.session.flush()

    db.session.add(SubscriptionPayment(
        vendor_id=user.id, subscription_id=sub.id, reference=reference or None,
        amount_ngn=amount_ngn, status="PAID",
    ))
    sync_verification_flag(user.id)
    db.session.commit()
    logger.info(f"[BILLING] {plan_name} payment {reference} for user {user.id}; active until {sub.end_date}")
    return sub


def remember_paystack_subscription(user, subscription_code, email_token):
    """Keep Paystack's subscription code so the plan can be stopped on refund."""
    from app.services.entitlements import active_subscription
    sub = active_subscription(user.id)
    if sub and subscription_code:
        sub.paystack_subscription_code = subscription_code
        sub.paystack_email_token = email_token
        db.session.commit()


def stop_auto_renew(user):
    """Seller turned off auto-renew: keep the plan until the paid period ends."""
    from app.models.admin import VendorSubscription
    VendorSubscription.query.filter_by(vendor_id=user.id, status="ACTIVE").update(
        {"status": "CANCELLED_PENDING_EXPIRY"})
    db.session.commit()


# ── verification link ─────────────────────────────────────────────────────

def sync_verification_flag(user_id):
    """The Verified mark shows only while the plan is active (lapse pauses it;
    the verification itself stays valid until its own expiry)."""
    from app.models.verification import VerificationStatus
    from app.services.entitlements import plan_active
    row = VerificationStatus.query.filter_by(user_id=user_id).first()
    if row is None:
        return
    active = plan_active(user_id)
    if row.subscription_active != active and row.source_provider != "mock":
        row.subscription_active = active


def mismatches_this_period(user_id):
    from app.models.verification import CaseStatus, VerificationCase
    from app.services.entitlements import active_subscription
    sub = active_subscription(user_id)
    if not sub:
        return 0
    since = _aware(sub.start_date) or _now()
    cases = VerificationCase.query.filter_by(user_id=user_id, status=CaseStatus.FAILED).all()
    return sum(1 for c in cases if c.created_at and _aware(c.created_at) >= since)


def handle_identity_mismatch(user_id):
    """Called after a check fails on mismatched details. After MISMATCH_LIMIT
    mismatches in the paid period, refund the latest payment and end the plan."""
    if mismatches_this_period(user_id) >= MISMATCH_LIMIT:
        return refund_and_end_plan(user_id, reason="IDENTITY_MISMATCH")
    return False


def refund_and_end_plan(user_id, reason):
    from app.models.billing import SubscriptionPayment
    from app.models.communication import Notification
    from app.services.entitlements import active_subscription

    sub = active_subscription(user_id)
    if not sub:
        return False
    payment = (SubscriptionPayment.query
               .filter_by(subscription_id=sub.id, status="PAID")
               .order_by(SubscriptionPayment.created_at.desc())
               .first())
    refunded = False
    if payment and payment.reference:
        refunded = _paystack_refund(payment.reference)
        payment.status = "REFUNDED" if refunded else "REFUND_FAILED"
        payment.refund_reason = reason
        payment.refunded_at = _now() if refunded else None
    if sub.paystack_subscription_code and sub.paystack_email_token:
        _paystack_disable(sub.paystack_subscription_code, sub.paystack_email_token)

    sub.status = "REFUNDED"
    sub.end_date = _now()
    sync_verification_flag(user_id)
    db.session.add(Notification(
        user_id=user_id,
        title="Verified Pro refunded",
        message=("We couldn't match your details after 3 tries, so we've refunded your Verified Pro "
                 "payment and moved your store to Free. Your store, products and trust score are unchanged."
                 if refunded else
                 "We couldn't match your details after 3 tries. Your plan has ended and our team is "
                 "processing your refund. Contact support@siiqo.com if you have questions."),
        type="ACCOUNT",
    ))
    db.session.commit()
    logger.info(f"[BILLING] Plan ended for user {user_id} ({reason}); refund ok={refunded}")
    return True


def _paystack_refund(reference):
    key = _paystack_key()
    if not key:
        logger.error("[BILLING] Refund skipped: PAYSTACK_SECRET_KEY not set")
        return False
    try:
        res = requests.post(f"{PAYSTACK_BASE_URL}/refund", json={"transaction": reference},
                            headers={"Authorization": f"Bearer {key}"}, timeout=15)
        return bool(res.ok and res.json().get("status"))
    except Exception as e:
        logger.error(f"[BILLING] Refund failed for {reference}: {e}")
        return False


def _paystack_disable(code, token):
    key = _paystack_key()
    if not key:
        return False
    try:
        res = requests.post(f"{PAYSTACK_BASE_URL}/subscription/disable", json={"code": code, "token": token},
                            headers={"Authorization": f"Bearer {key}"}, timeout=15)
        return bool(res.ok and res.json().get("status"))
    except Exception as e:
        logger.error(f"[BILLING] Disable subscription {code} failed: {e}")
        return False


# ── notices and daily upkeep ──────────────────────────────────────────────

def _notify_once(user, title, message, email_subject=None):
    from app.models.communication import Notification
    if Notification.query.filter_by(user_id=user.id, title=title).first():
        return False
    db.session.add(Notification(user_id=user.id, title=title, message=message, type="ACCOUNT"))
    if email_subject and user.email:
        try:
            from app.utils.email import send_siiqo_email
            send_siiqo_email(to_email=user.email, subject=email_subject, template_name="system_notice",
                             first_name=user.first_name or "there", notice_text=message)
        except Exception as e:
            logger.warning(f"[BILLING] Notice email failed for user {user.id}: {e}")
    return True


def send_verified_pro_notice():
    """One-time notice to current Pro subscribers: Pro is now Verified Pro.
    Safe to run more than once (each seller is notified once)."""
    from app.models.admin import VendorSubscription
    from app.models.user import User
    sent = 0
    subs = VendorSubscription.query.filter(
        VendorSubscription.status.in_(("ACTIVE", "CANCELLED_PENDING_EXPIRY"))).all()
    for sub in subs:
        user = db.session.get(User, sub.vendor_id)
        if user and _notify_once(
                user, "Pro is now Verified Pro",
                "Pro is becoming Verified Pro: ₦1,999/month or ₦19,990/year. You keep everything until your "
                "current period ends. From your next renewal you'll pay the new price, and once verification "
                "opens you can verify your identity to show the Verified mark and keep Pay on Delivery.",
                email_subject="Pro is now Verified Pro | Siiqo"):
            sent += 1
    db.session.commit()
    return sent


def run_daily_billing_tasks():
    """Renewal reminders and keeping the Verified mark in step with the plan."""
    from app.models.admin import VendorSubscription
    from app.models.user import User
    from app.models.verification import VerificationStatus

    now = _now()
    soon = now + timedelta(days=REMINDER_DAYS_BEFORE)
    for sub in VendorSubscription.query.filter_by(status="ACTIVE").all():
        end = _aware(sub.end_date)
        if end and now < end <= soon:
            user = db.session.get(User, sub.vendor_id)
            if user:
                _notify_once(user, f"Verified Pro renews on {end:%d %b %Y}",
                             f"Your Verified Pro plan renews on {end:%d %b %Y}. Make sure your card can be "
                             "charged so your Verified mark and Pro tools stay on.")
    for row in VerificationStatus.query.all():
        sync_verification_flag(row.user_id)
    db.session.commit()

    # Verification expiry reminders, risk re-checks and pause notices (2-3).
    # Same daily job and advisory lock; kept separate so a problem there never
    # undoes the billing work committed above.
    try:
        from app.services.verification.rechecks import run_verification_upkeep
        run_verification_upkeep(now)
    except Exception as e:
        db.session.rollback()
        logger.error(f"[BILLING] Verification upkeep failed: {e}")
