"""
Mandatory verification flag (D11): VERIFICATION_REQUIRED_FOR.

Values (read on every call, so it can be switched without a deploy):
  none                    default. Behaviour identical to before this flag.
  payouts_over:<amount>   a payout above ₦<amount> waits until the seller
                          verifies ("Verify to receive payouts above ₦X").
  new_stores              a store created while the flag is on starts
                          unpublished and its payouts wait until the seller
                          verifies ("Verify to keep selling").
  all_sellers             as new_stores for new stores; existing sellers get
                          VERIFICATION_REQUIRED_GRACE_DAYS (default 14), then
                          their payouts wait and they can't re-publish a store
                          until they verify. A live store is never taken down.

Rules that hold in every mode:
- Never retroactive: only orders paid after the seller's enforcement start
  are held (new store: from creation; existing seller: activation + grace).
  Money already earned pays out as before.
- Nothing is lost. A held payout is the FX-hold pattern from
  app/routes/escrow.py: a ledger DEBIT (`VERIFY-HOLD-…`) keeps it out of the
  withdrawable balance; when the seller verifies, the matching CREDIT
  (`VERIFY-RELEASE-…`) is written and the payout is sent through the same
  gateway function. If that send fails, the money stays withdrawable, as
  with any failed automatic payout today. Switching the flag back to none
  (or closing verification) releases everything still held at the next
  hourly sweep, and publishes stores that were waiting.
- Enforced only while identity verification is open to sellers
  (VERIFICATION_V2_ENABLED); otherwise nobody could comply, so nothing is
  held and the admin preview says why.
- "Verified" is the Passport's Verified fact (provider check with an active
  plan, or an unexpired legacy mark).
- The grace clock starts when the hourly sweep first sees the mode
  (platform_settings key `verification_required_activation`); until then
  existing sellers are not affected.
- Paystack/Flutterwave split settlements are paid by the gateway at
  checkout and never pass through a payout function, so they can't be held
  (D4: payment flows are unchanged).
"""
import json
import logging
import os
import re
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation

from app.extensions import db
from app.services import jobs

logger = logging.getLogger(__name__)

MODE_NONE, MODE_PAYOUTS_OVER, MODE_NEW_STORES, MODE_ALL = "none", "payouts_over", "new_stores", "all_sellers"
MODES = (MODE_NONE, MODE_PAYOUTS_OVER, MODE_NEW_STORES, MODE_ALL)
DEFAULT_GRACE_DAYS = 14
ACTIVATION_KEY = "verification_required_activation"
VERIFY_URL = "/vendor/trust"
HOLD_PREFIX, RELEASE_PREFIX = "VERIFY-HOLD-", "VERIFY-RELEASE-"
STEP_KEEP_SELLING = "Verify to keep selling"


def _now():
    return datetime.now(timezone.utc)


def _aware(dt):
    return dt.replace(tzinfo=timezone.utc) if dt is not None and dt.tzinfo is None else dt


def _iso(dt):
    return _aware(dt).isoformat() if dt else None


# ── configuration ──────────────────────────────────────────────────────────

def parse_mode(raw):
    """(mode, amount) from a VERIFICATION_REQUIRED_FOR value. Anything not
    understood is (none, None): the safe default is today's behaviour."""
    text = (raw or "").strip().lower()
    if text in ("", MODE_NONE):
        return MODE_NONE, None
    if text in (MODE_NEW_STORES, MODE_ALL):
        return text, None
    m = re.fullmatch(r"payouts_over:\s*₦?\s*([0-9][0-9,]*(?:\.[0-9]{1,2})?)", text)
    if m:
        try:
            amount = Decimal(m.group(1).replace(",", ""))
        except InvalidOperation:
            amount = None
        if amount is not None and amount >= 0:
            return MODE_PAYOUTS_OVER, amount
    logger.warning("[VERIFY REQUIRED] VERIFICATION_REQUIRED_FOR=%r not understood; treated as none", raw)
    return MODE_NONE, None


def current_mode():
    return parse_mode(os.environ.get("VERIFICATION_REQUIRED_FOR"))


def mode_label(mode, amount):
    return f"{MODE_PAYOUTS_OVER}:{amount}" if mode == MODE_PAYOUTS_OVER else mode


def grace_days():
    try:
        return max(0, int(os.environ.get("VERIFICATION_REQUIRED_GRACE_DAYS", DEFAULT_GRACE_DAYS)))
    except (TypeError, ValueError):
        return DEFAULT_GRACE_DAYS


def verification_open():
    from app.services.verification.service import is_public
    return is_public()


def enforced():
    """(mode, amount) when the requirement is in force, else (none, None)."""
    mode, amount = current_mode()
    if mode == MODE_NONE or not verification_open():
        return MODE_NONE, None
    return mode, amount


def _naira(amount):
    return f"₦{Decimal(amount):,.0f}" if amount == int(amount) else f"₦{Decimal(amount):,.2f}"


def step_title(mode, amount):
    if mode == MODE_PAYOUTS_OVER:
        return f"Verify to receive payouts above {_naira(amount)}"
    return STEP_KEEP_SELLING


# ── activation (grace clock) ───────────────────────────────────────────────

def _setting():
    from app.models.admin import PlatformSetting
    return PlatformSetting.query.filter_by(key=ACTIVATION_KEY).first()


def activation_at(mode=None, amount=None):
    """When the current mode was first seen by the sweep, or None."""
    if mode is None:
        mode, amount = current_mode()
    row = _setting()
    if row is None:
        return None
    try:
        data = json.loads(row.value)
        if data.get("mode") != mode_label(mode, amount):
            return None
        return _aware(datetime.fromisoformat(data["at"]))
    except (ValueError, KeyError, TypeError):
        return None


def record_activation(now=None):
    """Called by the hourly sweep: start the grace clock for a newly switched
    mode; forget it when the flag is back to none."""
    from app.models.admin import PlatformSetting
    now = now or _now()
    mode, amount = current_mode()
    row = _setting()
    if mode == MODE_NONE:
        if row is not None:
            db.session.delete(row)
            db.session.commit()
        return None
    if activation_at(mode, amount) is not None:
        return activation_at(mode, amount)
    value = json.dumps({"mode": mode_label(mode, amount), "at": now.isoformat()})
    if row is None:
        db.session.add(PlatformSetting(key=ACTIVATION_KEY, value=value,
                                       description="When VERIFICATION_REQUIRED_FOR took its current value"))
    else:
        row.value = value
    db.session.commit()
    return now


# ── who is affected ────────────────────────────────────────────────────────

def is_verified(vendor_id, now=None):
    """The Passport's Verified fact: provider check (active plan, not paused)
    or an unexpired legacy mark."""
    from app.models.user import Storefront
    from app.models.verification import VerificationStatus
    now = now or _now()
    status = VerificationStatus.query.filter_by(user_id=vendor_id).first()
    if status is not None and status.public_flags(now)["identity_verified"]:
        return True
    sf = Storefront.query.filter_by(vendor_id=vendor_id).first()
    if sf is not None and sf.is_pro_verified:
        expiry = _aware(sf.pro_verified_expires_at)
        return expiry is None or expiry > now
    return False


def _store_hold(vendor_id):
    from app.models.verification_requirement import VerificationHold
    return VerificationHold.query.filter_by(vendor_id=vendor_id, kind="store").first()


def _is_new_store(vendor_id, sf, activation):
    if _store_hold(vendor_id) is not None:
        return True
    created = _aware(getattr(sf, "created_at", None)) if sf is not None else None
    return activation is not None and created is not None and created >= activation


def enforcement_start(vendor_id, mode=None, amount=None):
    """From when this seller's new payouts wait for verification (None = not
    affected in this mode)."""
    from app.models.user import Storefront
    if mode is None:
        mode, amount = enforced()
    if mode == MODE_NONE:
        return None
    sf = Storefront.query.filter_by(vendor_id=vendor_id).first()
    activation = activation_at(mode, amount)
    if _is_new_store(vendor_id, sf, activation):
        created = _aware(getattr(sf, "created_at", None)) if sf is not None else None
        return created or activation or _now()
    if mode == MODE_NEW_STORES or activation is None:
        return None            # existing seller: new_stores never applies; others wait for the clock
    return activation + timedelta(days=grace_days())


# ── payouts ────────────────────────────────────────────────────────────────

def _paid_at(order, escrow):
    return _aware(getattr(order, "paid_at", None)) or _aware(getattr(escrow, "paid_at", None)) or _now()


def payout_needs_verification(order, escrow, net_amount, now=None):
    mode, amount = enforced()
    if mode == MODE_NONE or order is None:
        return False
    if mode == MODE_PAYOUTS_OVER and Decimal(str(net_amount or 0)) <= amount:
        return False
    start = enforcement_start(order.vendor_id, mode, amount)
    if start is None or _paid_at(order, escrow) < start:
        return False           # never retroactive
    return not is_verified(order.vendor_id, now)


def _hold_reference(order, escrow):
    return f"{HOLD_PREFIX}{getattr(escrow, 'transaction_number', None) or order.id}"


def hold_payout(order, escrow, net_amount):
    """Called by every automatic payout function before money moves. True
    when the payout must wait for verification (it is recorded as held and
    kept out of the withdrawable balance). With the flag off: one env read,
    then False. Never raises: a guard failure lets the payout go as today."""
    try:
        mode, amount = current_mode()
        if mode == MODE_NONE:
            return False
        from app.models.verification_requirement import VerificationHold
        existing = VerificationHold.query.filter_by(kind="payout", order_id=order.id).first()
        if existing is not None:
            return existing.status == "held"
        if not payout_needs_verification(order, escrow, net_amount):
            return False
        mode, amount = enforced()
        from app.models.communication import Notification
        from app.routes.escrow import _debit_vendor_ledger
        reference = _hold_reference(order, escrow)
        hold = VerificationHold(
            vendor_id=order.vendor_id, kind="payout", order_id=order.id,
            amount_ngn=Decimal(str(net_amount)).quantize(Decimal("0.01")), reference=reference,
            mode=mode_label(mode, amount), status="held")
        note = Notification(
            user_id=order.vendor_id, title=step_title(mode, amount),
            message=(f"Your payout for Order #{order.id} ({_naira(Decimal(str(net_amount)))}) is safe and "
                     "waiting. Verify your identity on the Trust tab and it will be sent automatically."),
            type="ESCROW", order_id=order.id)
        db.session.add(hold)
        db.session.add(note)
        try:
            # Last: keeps the held amount out of the withdrawable balance.
            _debit_vendor_ledger(
                vendor_id=order.vendor_id, amount=float(net_amount), reference_id=reference,
                description=f"Waiting for verification: Order #{order.id}")
        except Exception:
            # Undo the half-made hold so the payout goes ahead as before.
            for obj in (hold, note):
                if obj in db.session.new:
                    db.session.expunge(obj)
            raise
        logger.info("[VERIFY REQUIRED] payout for Order #%s held until seller %s verifies", order.id,
                    order.vendor_id)
        return True
    except Exception as e:  # noqa: BLE001 — never block a payout by accident
        logger.error("[VERIFY REQUIRED] payout guard error for Order #%s: %s", getattr(order, "id", None), e)
        return False


def _dispatch(order, escrow):
    """Send a released hold through the same gateway function the original
    payout used. True when the gateway accepted it."""
    from app.services.fx import is_daya_paid, is_flutterwave_paid
    from app.routes import escrow as escrow_routes
    if is_flutterwave_paid(order, escrow):
        return bool(escrow_routes._flutterwave_payout_vendor(order, escrow))
    if is_daya_paid(order, escrow):
        from app.routes.payments import _payout_vendor_via_daya
        return bool(_payout_vendor_via_daya(order, escrow))
    return bool(escrow_routes._paystack_payout_vendor(order, escrow))


def release_for(vendor_id, now=None, force=False):
    """Release everything held for a seller who is now verified (or, with
    force, because the requirement is no longer in force). Returns
    {"payouts": n, "dispatched": n, "stores": n}. Safe to run again."""
    from app.models.escrow import EscrowTransaction
    from app.models.order import Order
    from app.models.user import Storefront
    from app.models.verification_requirement import VerificationHold
    from app.routes.escrow import _credit_vendor_ledger, _record_payout_debit
    now = now or _now()
    out = {"payouts": 0, "dispatched": 0, "stores": 0}
    if not force and not is_verified(vendor_id, now):
        return out
    for hold in VerificationHold.query.filter_by(vendor_id=vendor_id, status="held").order_by(VerificationHold.id).all():
        if hold.kind == "store":
            sf = db.session.get(Storefront, hold.storefront_id) if hold.storefront_id else \
                Storefront.query.filter_by(vendor_id=vendor_id).first()
            if sf is not None:
                sf.is_published = True
            hold.status, hold.released_at, hold.release_result = "released", now, "published"
            db.session.commit()
            out["stores"] += 1
            continue
        order = db.session.get(Order, hold.order_id)
        escrow = EscrowTransaction.query.filter_by(order_id=hold.order_id).first()
        amount = float(hold.amount_ngn or 0)
        _credit_vendor_ledger(
            vendor_id=vendor_id, amount=amount,
            reference_id=f"{RELEASE_PREFIX}{hold.reference[len(HOLD_PREFIX):]}",
            description=f"Verified: payout released for Order #{hold.order_id}")
        hold.status, hold.released_at, hold.release_result = "released", now, "balance"
        db.session.commit()
        out["payouts"] += 1
        if order is None or escrow is None:
            continue
        try:
            if _dispatch(order, escrow):
                _record_payout_debit(order, escrow, amount)
                hold.release_result = "dispatched"
                out["dispatched"] += 1
            db.session.commit()
        except Exception as e:  # noqa: BLE001 — the money stays withdrawable
            db.session.rollback()
            logger.warning("[VERIFY REQUIRED] release send failed for Order #%s: %s", hold.order_id, e)
    return out


def notify_verified(vendor_id):
    """Hook for the verification code: queue a release when something is
    held. Never raises."""
    try:
        from app.models.verification_requirement import VerificationHold
        if VerificationHold.query.filter_by(vendor_id=vendor_id, status="held").first() is None:
            return None
        return jobs.enqueue("verification_holds_release", {"vendor_id": int(vendor_id)}, user_id=int(vendor_id))
    except Exception as e:  # noqa: BLE001
        try:
            db.session.rollback()
        except Exception:  # noqa: BLE001
            pass
        logger.warning("[VERIFY REQUIRED] release for seller %s not queued: %s", vendor_id, e)
        return None


@jobs.handler("verification_holds_release")
def _release_job(payload, job):
    return release_for(int(payload["vendor_id"]))


def run_verification_requirement_sweep(now=None):
    """Hourly: start the grace clock for a new mode, then release holds of
    sellers who have verified since. When the requirement is no longer in
    force (flag back to none, or verification closed), everything still held
    is released, so switching the flag off restores today's behaviour."""
    from app.models.verification_requirement import VerificationHold
    now = now or _now()
    record_activation(now)
    force = enforced()[0] == MODE_NONE
    vendor_ids = sorted({v for (v,) in db.session.query(VerificationHold.vendor_id)
                         .filter(VerificationHold.status == "held").distinct().all()})
    released = 0
    for vid in vendor_ids:
        try:
            r = release_for(vid, now, force=force)
            released += r["payouts"] + r["stores"]
        except Exception as e:  # noqa: BLE001
            db.session.rollback()
            logger.warning("[VERIFY REQUIRED] sweep failed for seller %s: %s", vid, e)
    return {"sellers": len(vendor_ids), "released": released}


run_sweep = run_verification_requirement_sweep


# ── publishing ─────────────────────────────────────────────────────────────

def new_store_requirement(user):
    """Before creating a store: the step to show when the new store must
    start unpublished, else None."""
    mode, amount = enforced()
    if mode not in (MODE_NEW_STORES, MODE_ALL) or user is None or is_verified(user.id):
        return None
    return {"code": "VERIFICATION_REQUIRED", "title": STEP_KEEP_SELLING,
            "message": "Your store is saved. Verify your identity to publish it and start selling.",
            "action_url": VERIFY_URL}


def record_new_store_hold(vendor_id, storefront_id):
    from app.models.verification_requirement import VerificationHold
    mode, amount = enforced()
    if _store_hold(vendor_id) is None:
        db.session.add(VerificationHold(vendor_id=vendor_id, kind="store", storefront_id=storefront_id,
                                        mode=mode_label(mode, amount), status="held"))


def publish_blocked(vendor_id, sf=None):
    """For a seller turning their store on: the step to show, or None."""
    mode, amount = enforced()
    if mode not in (MODE_NEW_STORES, MODE_ALL) or is_verified(vendor_id):
        return None
    start = enforcement_start(vendor_id, mode, amount)
    if start is None or start > _now():
        return None
    return {"code": "VERIFICATION_REQUIRED", "title": STEP_KEEP_SELLING,
            "message": "Verify your identity to publish your store.", "action_url": VERIFY_URL}


# ── what the seller sees ───────────────────────────────────────────────────

def seller_status(vendor_id):
    """One clear step for the seller (or required: false)."""
    from sqlalchemy import func
    from app.models.verification_requirement import VerificationHold
    mode, amount = enforced()
    held = db.session.query(func.count(VerificationHold.id), func.coalesce(func.sum(VerificationHold.amount_ngn), 0)) \
        .filter(VerificationHold.vendor_id == vendor_id, VerificationHold.kind == "payout",
                VerificationHold.status == "held").one()
    store_held = VerificationHold.query.filter_by(vendor_id=vendor_id, kind="store", status="held").first() is not None
    body = {"mode": mode, "required": False, "verified": None, "step": None, "grace_ends_at": None,
            "held_payouts": {"count": int(held[0] or 0), "amount_ngn": float(held[1] or 0)},
            "store_held": store_held}
    if mode == MODE_NONE:
        return body
    verified = is_verified(vendor_id)
    body["verified"] = verified
    start = enforcement_start(vendor_id, mode, amount)
    if verified or start is None:
        return body
    body["required"] = True
    body["grace_ends_at"] = _iso(start) if start > _now() else None
    body["step"] = {"title": step_title(mode, amount), "action_url": VERIFY_URL,
                    "message": ("Payouts above this amount wait safely until you verify."
                                if mode == MODE_PAYOUTS_OVER else
                                "Your earnings are safe. New payouts wait until you verify.")}
    if mode == MODE_PAYOUTS_OVER:
        body["payout_limit_ngn"] = float(amount)
    return body


# ── superadmin preview ─────────────────────────────────────────────────────

def preview(raw_mode=None, now=None):
    """How many sellers a mode would affect, before switching it on."""
    from sqlalchemy import func
    from app.models.escrow import EscrowStatus, EscrowTransaction
    from app.models.order import Order
    from app.models.user import Storefront
    from app.models.verification_requirement import VerificationHold
    now = now or _now()
    current, current_amount = current_mode()
    mode, amount = parse_mode(raw_mode) if raw_mode is not None else (current, current_amount)
    vendor_ids = [v for (v,) in db.session.query(Storefront.vendor_id).distinct().all()]
    unverified = [v for v in vendor_ids if not is_verified(v, now)]
    out = {
        "mode": mode_label(mode, amount),
        "current_mode": mode_label(current, current_amount),
        "verification_open": verification_open(),
        "grace_days": grace_days(),
        "activation_at": _iso(activation_at(current, current_amount)),
        "sellers_with_store": len(vendor_ids),
        "unverified_sellers": len(unverified),
        "affected_sellers": 0,
        "held_now": {
            "payouts": VerificationHold.query.filter_by(kind="payout", status="held").count(),
            "amount_ngn": float(db.session.query(func.coalesce(func.sum(VerificationHold.amount_ngn), 0))
                                .filter(VerificationHold.kind == "payout", VerificationHold.status == "held")
                                .scalar() or 0),
            "stores": VerificationHold.query.filter_by(kind="store", status="held").count(),
        },
        "notes": [],
    }
    if not out["verification_open"]:
        out["notes"].append("Identity verification is not open to sellers (VERIFICATION_V2_ENABLED), "
                            "so nothing is enforced until it is.")
    if mode == MODE_NONE:
        return out
    if mode == MODE_NEW_STORES:
        out["affected_sellers"] = 0
        out["notes"].append("Only stores created after switching are affected; existing sellers are not.")
        return out
    if mode == MODE_ALL:
        out["affected_sellers"] = len(unverified)
        out["notes"].append(f"Existing unverified sellers get {grace_days()} days before new payouts wait.")
        return out
    # payouts_over: unverified sellers with at least one payout above the
    # amount in the last 90 days (what would have waited).
    since = now - timedelta(days=90)
    rows = (db.session.query(Order.vendor_id, EscrowTransaction.amount, EscrowTransaction.fee_amount)
            .join(EscrowTransaction, EscrowTransaction.order_id == Order.id)
            .filter(EscrowTransaction.status == EscrowStatus.RELEASED,
                    EscrowTransaction.released_at >= since,
                    Order.vendor_id.in_(unverified or [-1])).all())
    over = {v for v, amt, fee in rows if Decimal(str(amt or 0)) - Decimal(str(fee or 0)) > amount}
    out["affected_sellers"] = len(over)
    out["payout_limit_ngn"] = float(amount)
    out["notes"].append("Affected = unverified sellers with a payout above the limit in the last 90 days.")
    return out
