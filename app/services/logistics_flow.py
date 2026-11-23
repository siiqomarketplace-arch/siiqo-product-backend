"""The life of a delivery job (LogisticsAssignment).

    PENDING      created at checkout, waiting for the buyer's payment
    ASSIGNED     payment confirmed (order.paid_at) or Pay on Delivery: the
                 partner is notified and should collect the item
    PICKED_UP    the rider has the item (vendor and buyer are told)
    IN_TRANSIT   on the way
    DELIVERED    handed over; needs the buyer's delivery code (vendor and buyer
                 are told; Safe Pay's 72h auto-release clock starts)
    CANCELLED    the order was cancelled or refunded before delivery

Activation and cancellation run from SQLAlchemy flush hooks, so every
payment path (Paystack, Flutterwave, Daya, admin) triggers them the same way.
Emails are queued and sent only after the transaction commits.
"""
from __future__ import annotations

import hashlib
import hmac
import logging
import os
import secrets
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from sqlalchemy import event, inspect
from sqlalchemy.orm import Session

from app.extensions import db

log = logging.getLogger(__name__)

PENDING, ASSIGNED, PICKED_UP, IN_TRANSIT, DELIVERED, CANCELLED = (
    "PENDING", "ASSIGNED", "PICKED_UP", "IN_TRANSIT", "DELIVERED", "CANCELLED")
ACTIVE_STATUSES = (ASSIGNED, PICKED_UP, IN_TRANSIT)
STATUS_LABELS = {PENDING: "Waiting for payment", ASSIGNED: "Ready for pickup", PICKED_UP: "Picked up",
                 IN_TRANSIT: "On the way", DELIVERED: "Delivered", CANCELLED: "Cancelled"}

# Which status may follow which (a rider at the door may skip the middle steps).
_NEXT = {
    ASSIGNED: (PICKED_UP, IN_TRANSIT, DELIVERED),
    PICKED_UP: (IN_TRANSIT, DELIVERED),
    IN_TRANSIT: (DELIVERED,),
}

CODE_MAX_ATTEMPTS = 5
CODE_WINDOW = timedelta(hours=1)

_ORDER_CANCEL_STATUSES = ("CANCELLED", "REFUNDED")
_ESCROW_CANCEL_STATUSES = ("CANCELLED", "REFUNDED")
_MAIL_KEY = "_logistics_mail"
_REGISTERED = False


def _now():
    return datetime.now(timezone.utc)


def _aware(dt):
    return dt.replace(tzinfo=timezone.utc) if dt is not None and dt.tzinfo is None else dt


def _site():
    return (os.environ.get("FRONTEND_URL") or "https://siiqo.com").rstrip("/")


class FlowError(Exception):
    def __init__(self, code, message, status=400, **extra):
        super().__init__(message)
        self.code, self.message, self.status, self.extra = code, message, status, extra

    def response(self):
        from flask import jsonify
        return jsonify({"code": self.code, "message": self.message, **self.extra}), self.status


# ── notifications ──────────────────────────────────────────────────────────

def _queue_email(session, **kwargs):
    if kwargs.get("to_email"):
        session.info.setdefault(_MAIL_KEY, []).append(kwargs)


def _notice_email(session, to_email, subject, first_name, text, cta_url=None, cta_label=None):
    _queue_email(session, to_email=to_email, subject=subject, template_name="system_notice",
                 first_name=first_name or "there", notice_text=text,
                 **({"cta_url": cta_url, "cta_label": cta_label} if cta_url else {}))


def _notify(session, user_id, title, message, order_id=None, kind="DELIVERY"):
    """In-app notification. Never for user_id None (guests get email)."""
    if not user_id:
        return
    from app.models.communication import Notification
    session.add(Notification(user_id=user_id, title=title, message=message, type=kind, order_id=order_id))


def _buyer_email(order):
    from app.tasks.escrow_tasks import real_email
    buyer = getattr(order, "buyer", None)
    return real_email((buyer.email if buyer else None) or getattr(order, "buyer_email", None))


def _buyer_first_name(order):
    buyer = getattr(order, "buyer", None)
    name = (buyer.first_name if buyer else None) or getattr(order, "buyer_name", None) or "there"
    return str(name).split(" ")[0]


def _store_name(order):
    try:
        sf = order.vendor.storefront if order.vendor else None
        return sf.store_name if sf else "the seller"
    except Exception:  # noqa: BLE001
        return "the seller"


def _partner_name(assignment):
    from app.services.logistics_partners import latest_application
    app_row = latest_application(assignment.partner_id)
    if app_row and app_row.business_name:
        return app_row.business_name
    return assignment.partner.full_name if assignment.partner else "Your delivery partner"


def _order_link(order):
    try:
        from app.routes.escrow import generate_order_token
        return f"{_site()}/order-confirm/{order.id}?token={generate_order_token(order.id)}"
    except Exception:  # noqa: BLE001
        return None


def _tell_buyer(session, order, title, text):
    """In-app for buyers with an account; email for guests."""
    if order.buyer_id and not order.is_guest:
        _notify(session, order.buyer_id, title, text.replace("<br>", " "), order.id, kind="ORDER")
        return
    email = _buyer_email(order)
    if email:
        _notice_email(session, email, f"{title} - Order #{order.id}", _buyer_first_name(order), text,
                      cta_url=_order_link(order), cta_label="View your order")


def _send_queued(session):
    pending = session.info.pop(_MAIL_KEY, None)
    if not pending:
        return
    from app.utils import email as email_mod
    for kwargs in pending:
        try:
            email_mod.send_siiqo_email(**kwargs)
        except Exception as e:  # noqa: BLE001 - a failed email never undoes the change
            log.warning("[LOGISTICS] email not sent: %s", e)


def _drop_queued(session, *args):
    session.info.pop(_MAIL_KEY, None)


# ── create / activate / cancel ─────────────────────────────────────────────

def create_assignment(order, quote, assign_now=False):
    """The delivery job for a checkout option priced by the server."""
    from app.models.escrow import LogisticsAssignment
    assignment = LogisticsAssignment(
        order_id=order.id, partner_id=quote.provider_id, provider_type=quote.provider_type,
        status=PENDING, delivery_fee=Decimal(str(quote.provider_fee)))
    db.session.add(assignment)
    if assign_now:
        activate(db.session, assignment, order)
    return assignment


def activate(session, assignment, order):
    """Payment is confirmed: the partner should collect the item."""
    if assignment.status != PENDING:
        return False
    assignment.status = ASSIGNED
    assignment.assigned_at = _now()
    store = _store_name(order)
    city = ", ".join(x for x in (order.delivery_city, order.delivery_state) if x)
    msg = (f"New delivery for Order #{order.id}: pick up from {store}"
           + (f", deliver to {city}" if city else "")
           + f". You earn NGN {float(assignment.delivery_fee or 0):,.2f}.")
    _notify(session, assignment.partner_id, "New delivery job", msg, order.id)
    partner = assignment.partner
    if partner is None and assignment.partner_id:
        from app.models.user import User
        partner = session.get(User, assignment.partner_id)
    if partner and partner.email:
        _notice_email(session, partner.email, f"New delivery job - Order #{order.id}",
                      partner.first_name, msg + "<br><br>Open your partner dashboard to assign a rider.",
                      cta_url=f"{_site()}/partners/dashboard/orders", cta_label="Open dashboard")
    return True


def cancel(session, assignment, order, reason="cancelled"):
    if assignment.status in (DELIVERED, CANCELLED):
        return False
    was_active = assignment.status in ACTIVE_STATUSES
    assignment.status = CANCELLED
    assignment.cancelled_at = _now()
    if was_active:
        _notify(session, assignment.partner_id, "Delivery cancelled",
                f"Order #{order.id} was {reason}. Don't collect or deliver it.", order.id)
        if assignment.rider_user_id:
            _notify(session, assignment.rider_user_id, "Delivery cancelled",
                    f"Order #{order.id} was {reason}. Don't collect or deliver it.", order.id)
    return True


def _first_paid(order):
    if order.paid_at is None:
        return False
    state = inspect(order)
    if state.pending:
        return True
    hist = state.attrs.paid_at.history
    return bool(hist.added) and hist.added[0] is not None and not any(d is not None for d in hist.deleted)


def _before_flush(session, flush_context, instances):
    try:
        with session.no_autoflush:
            _apply_payment_and_cancel(session)
    except Exception as e:  # never block the payment that triggered this
        log.warning("[LOGISTICS] flush hook skipped: %s", e)


def _apply_payment_and_cancel(session):
    from app.models.escrow import EscrowTransaction, LogisticsAssignment
    from app.models.order import Order
    orders_paid, orders_cancelled = {}, {}
    for obj in list(session.new) + list(session.dirty):
        if isinstance(obj, Order):
            if _first_paid(obj):
                orders_paid[obj.id] = obj
            hist = inspect(obj).attrs.status.history
            if hist.added and (hist.added[0] or "").upper() in _ORDER_CANCEL_STATUSES:
                orders_cancelled[obj.id] = (obj, (hist.added[0] or "").lower())
        elif isinstance(obj, EscrowTransaction) and obj.order_id:
            hist = inspect(obj).attrs.status.history
            if hist.added and (hist.added[0] or "").upper() in _ESCROW_CANCEL_STATUSES:
                order = obj.order or session.get(Order, obj.order_id)
                if order is not None:
                    orders_cancelled[order.id] = (order, "refunded" if hist.added[0] == "REFUNDED"
                                                  else "cancelled")
    if not orders_paid and not orders_cancelled:
        return
    ids = [i for i in set(orders_paid) | set(orders_cancelled) if i]
    if not ids:
        return
    rows = LogisticsAssignment.query.filter(LogisticsAssignment.order_id.in_(ids)).all()
    for assignment in rows:
        if assignment.order_id in orders_cancelled:
            order, reason = orders_cancelled[assignment.order_id]
            cancel(session, assignment, order, reason)
        elif assignment.order_id in orders_paid:
            activate(session, assignment, orders_paid[assignment.order_id])


def register_hooks():
    """Attach once, after the foundations hook (which stamps order.paid_at)."""
    global _REGISTERED
    if _REGISTERED:
        return
    event.listen(Session, "before_flush", _before_flush)
    event.listen(Session, "after_commit", _send_queued)
    event.listen(Session, "after_soft_rollback", _drop_queued)
    _REGISTERED = True


# ── who may act on a job ───────────────────────────────────────────────────

def actor_for(assignment, user):
    """'partner' or 'rider' when this user may move the job, else None."""
    from app.models.user import UserRole
    from app.services.logistics_partners import partner_access
    if user is None or assignment is None:
        return None
    if user.role == UserRole.PARTNER and assignment.partner_id == user.id:
        return "partner" if partner_access(user).ok else None
    if (user.role == UserRole.RIDER and user.is_active and assignment.rider_user_id
            and assignment.rider_user_id == user.id):
        from app.models.user import User
        partner = db.session.get(User, assignment.partner_id)
        return "rider" if partner_access(partner).ok else None
    return None


# ── delivery code ──────────────────────────────────────────────────────────

def _handover_hash(assignment_id, code):
    secret = (os.environ.get("SECRET_KEY") or "siiqo-secret-key-salt-2026").encode()
    return hmac.new(secret, f"handover-{assignment_id}-{code}".encode(), hashlib.sha256).hexdigest()


def _new_handover_code(assignment):
    code = f"{secrets.randbelow(1_000_000):06d}"
    assignment.handover_code_hash = _handover_hash(assignment.id, code)
    return code


def _expected_code(order, assignment):
    """('escrow', code) | ('handover', None) | (None, None)."""
    escrow = order.escrow
    if escrow is not None:
        return "escrow", str(escrow.escrow_code or "").strip()
    if assignment.handover_code_hash:
        return "handover", None
    return None, None


def _pod_cash_confirmed(order):
    from app.models.withdrawal import PODPayment
    pod = PODPayment.query.filter_by(order_id=order.id).first()
    return bool(pod and pod.confirmed_by_vendor)


def _check_code(assignment, order, provided):
    """Raise FlowError unless the buyer's code is right. Wrong guesses are
    counted (CODE_MAX_ATTEMPTS per hour per job), then the job is locked."""
    now = _now()
    locked = _aware(assignment.code_locked_until)
    if locked and locked > now:
        mins = max(1, int((locked - now).total_seconds() // 60))
        raise FlowError("CODE_LOCKED", f"Too many wrong codes. Try again in {mins} minutes, "
                                       "or ask the buyer to contact Siiqo support.", 429)
    kind, expected = _expected_code(order, assignment)
    if kind is None:
        if (order.payment_method or "").upper() == "POD" and _pod_cash_confirmed(order):
            return
        raise FlowError("NEEDS_PICKUP", "Mark the order as picked up first. The buyer then gets "
                                        "a delivery code to give the rider.", 409)
    provided = str(provided or "").strip()
    if kind == "handover" and not provided and _pod_cash_confirmed(order):
        return
    if not provided:
        raise FlowError("CODE_REQUIRED", "Ask the buyer for their delivery code and enter it.", 400)
    if kind == "escrow":
        good = bool(expected) and hmac.compare_digest(provided, expected)
    else:
        good = hmac.compare_digest(_handover_hash(assignment.id, provided), assignment.handover_code_hash)
    if good:
        assignment.code_attempts = 0
        assignment.code_window_started_at = None
        assignment.code_locked_until = None
        return
    start = _aware(assignment.code_window_started_at)
    if start is None or now - start > CODE_WINDOW:
        assignment.code_window_started_at = now
        assignment.code_attempts = 0
    assignment.code_attempts = (assignment.code_attempts or 0) + 1
    left = CODE_MAX_ATTEMPTS - assignment.code_attempts
    if left <= 0:
        assignment.code_locked_until = now + CODE_WINDOW
        log.warning("[SECURITY] delivery code locked: assignment %s order %s after %s wrong codes",
                    assignment.id, order.id, assignment.code_attempts)
        alert = (f"Someone entered a wrong delivery code {assignment.code_attempts} times for Order #{order.id}. "
                 "Delivery confirmation is locked for 1 hour.")
        _notify(db.session, assignment.partner_id, "Delivery code locked", alert, order.id)
        _notify(db.session, order.vendor_id, "Delivery code locked", alert, order.id, kind="ORDER")
        db.session.commit()
        raise FlowError("CODE_LOCKED", "Too many wrong codes. Delivery confirmation is locked for 1 hour.", 429)
    db.session.commit()
    raise FlowError("WRONG_CODE", f"That code is not correct. {left} "
                                  f"{'try' if left == 1 else 'tries'} left.", 400, attempts_left=left)


# ── moving a job ───────────────────────────────────────────────────────────

def _valid_proof_url(url):
    if not url:
        return None
    url = str(url).strip()[:500]
    from flask import current_app
    bucket = current_app.config.get("AWS_S3_BUCKET_NAME")
    region = current_app.config.get("AWS_REGION", "us-east-1")
    allowed = ["/static/uploads/delivery_proof/"]
    if bucket:
        allowed.append(f"https://{bucket}.s3.{region}.amazonaws.com/uploads/delivery_proof/")
    if any(url.startswith(p) for p in allowed) and ".." not in url:
        return url
    raise FlowError("INVALID_PROOF", "Upload the delivery photo again.", 400)


def move(assignment, new_status, user, data=None):
    """Move a job to its next status. Commits. Raises FlowError."""
    from app.models.escrow import EscrowStatus
    from app.services.disputes import payout_blocked

    data = data or {}
    new_status = (new_status or "").upper().strip()
    if actor_for(assignment, user) is None:
        raise FlowError("FORBIDDEN", "You can't update this delivery.", 403)
    order = assignment.order
    current = assignment.status
    if new_status not in (PICKED_UP, IN_TRANSIT, DELIVERED):
        raise FlowError("INVALID_STATUS", "Status must be PICKED_UP, IN_TRANSIT or DELIVERED.", 400)
    if current == PENDING:
        raise FlowError("NOT_PAID", "The buyer hasn't paid yet. Wait for the job to be assigned.", 409)
    if current == new_status:
        raise FlowError("ALREADY", f"This delivery is already {STATUS_LABELS[current].lower()}.", 409)
    if new_status not in _NEXT.get(current, ()):
        raise FlowError("INVALID_STATUS",
                        f"This delivery is {STATUS_LABELS.get(current, current).lower()}; it can't move to "
                        f"{STATUS_LABELS[new_status].lower()}.", 409)
    escrow = order.escrow if order else None
    if order is None:
        raise FlowError("NOT_FOUND", "Order not found.", 404)
    if escrow is not None and payout_blocked(order, escrow):
        raise FlowError("DISPUTE_OPEN", "This order has an open dispute. Don't deliver it until Siiqo resolves it.",
                        409)
    if escrow is not None and escrow.status in (EscrowStatus.PENDING_PAYMENT, "CANCELLED", "REFUNDED"):
        raise FlowError("NOT_PAID", "This order isn't paid (or was refunded). Don't deliver it.", 409)

    now = _now()
    store = _store_name(order)
    partner_name = _partner_name(assignment)
    after_commit = []

    if new_status == PICKED_UP:
        assignment.status = PICKED_UP
        assignment.picked_up_at = now
        if escrow is not None:
            if escrow.status == EscrowStatus.IN_ESCROW:
                escrow.status = EscrowStatus.SHIPPED
            if (order.status or "").upper() in ("PENDING", "PAID", "PROCESSING"):
                order.status = "SHIPPED"
            code_line = (f"Your delivery code is <strong>{escrow.escrow_code}</strong>. "
                         "Give it to the rider only when you have your item." if escrow.escrow_code else "")
        else:
            code_line = (f"Your delivery code is <strong>{_new_handover_code(assignment)}</strong>. "
                         "Give it to the rider only when you have your item.")
        _notify(db.session, order.vendor_id, "Order picked up",
                f"{partner_name} picked up Order #{order.id}.", order.id, kind="ORDER")
        _tell_buyer(db.session, order, "Your order is on the way",
                    f"{partner_name} has picked up your order #{order.id} from {store}. {code_line}")

    elif new_status == IN_TRANSIT:
        assignment.status = IN_TRANSIT
        assignment.in_transit_at = now
        if escrow is not None and escrow.status == EscrowStatus.IN_ESCROW:
            escrow.status = EscrowStatus.SHIPPED

    else:  # DELIVERED
        proof = _valid_proof_url(data.get("proof_photo_url"))
        _check_code(assignment, order, data.get("delivery_code") or data.get("delivery_otp"))
        assignment.status = DELIVERED
        assignment.delivered_at = now
        if proof:
            assignment.proof_photo_url = proof
        # Same effect as the seller marking it delivered (vendor.update_order_status):
        # order DELIVERED, a held payment moves to DELIVERED, and the flush hook
        # stamps order.delivered_at so the Safe Pay auto-release clock starts.
        if (order.status or "").upper() not in ("COMPLETED", "DELIVERED"):
            order.status = "DELIVERED"
        if escrow is not None and escrow.status in (EscrowStatus.IN_ESCROW, EscrowStatus.SHIPPED):
            escrow.status = EscrowStatus.DELIVERED
            after_commit.append("confirm_by")
        _notify(db.session, order.vendor_id, "Order delivered",
                f"{partner_name} delivered Order #{order.id}.", order.id, kind="ORDER")
        if escrow is None:
            _tell_buyer(db.session, order, "Your order was delivered",
                        f"{partner_name} delivered your order #{order.id} from {store}. Thank you for using Siiqo.")
        elif order.buyer_id and not order.is_guest:
            _notify(db.session, order.buyer_id, "Your order was delivered",
                    f"Order #{order.id} was delivered. Please confirm you received it to release payment "
                    "to the seller, or report a problem.", order.id, kind="ORDER")
        # Guests with Safe Pay get the confirm-by email below (as when the seller marks it).

    db.session.commit()

    if "confirm_by" in after_commit:
        try:
            from app.services.trust import release_deadline
            from app.tasks.escrow_tasks import email_buyer_confirm_by
            deadline = release_deadline(escrow)
            if deadline is not None:
                email_buyer_confirm_by(order, deadline)
        except Exception as e:  # noqa: BLE001 - the delivery is already saved
            log.info("[LOGISTICS] confirm-by email not sent for order %s: %s", order.id, e)
    return assignment


# ── earnings ───────────────────────────────────────────────────────────────

def _is_pod(order):
    return (getattr(order, "payment_method", None) or "").upper() == "POD"


def _earned(assignment):
    """A delivery fee is earned (paid by Siiqo) once the job is DELIVERED and
    the buyer's Safe Pay money is final:
      * escrow RELEASED; or
      * escrow REFUNDED after delivery: under the delivery refund rule the
        buyer gets the item back and the partner keeps the delivery fee
        (app/services/delivery_money.py). A refund before delivery cancels
        the job instead, so it never reaches here.
    Pay on Delivery jobs never create Siiqo-paid earnings: the buyer paid the
    seller in cash and Siiqo never held the delivery fee. Checkout no longer
    lets POD orders use a delivery partner; older POD jobs are listed on the
    admin Delivery money screen."""
    order = assignment.order
    if assignment.status != DELIVERED or order is None or _is_pod(order):
        return False
    escrow = order.escrow
    if escrow is None:
        return False
    return escrow.status in ("RELEASED", "REFUNDED")


def partner_earnings(partner_id):
    """{'earned', 'pending', 'withdrawn', 'available', 'completed_deliveries'} as Decimals/ints."""
    from sqlalchemy import func
    from app.models.escrow import LogisticsAssignment
    from app.models.withdrawal import PartnerWithdrawal
    earned = pending = Decimal("0")
    completed = 0
    rows = LogisticsAssignment.query.filter(
        LogisticsAssignment.partner_id == int(partner_id),
        LogisticsAssignment.status.in_((ASSIGNED, PICKED_UP, IN_TRANSIT, DELIVERED))).all()
    for a in rows:
        fee = Decimal(str(a.delivery_fee or 0))
        order = a.order
        if _earned(a):
            earned += fee
            completed += 1
        elif (order is not None and not _is_pod(order)
              and (order.status or "").upper() not in ("CANCELLED", "REFUNDED")
              and not (order.escrow is not None and order.escrow.status in ("REFUNDED", "CANCELLED"))):
            pending += fee          # on the way, or delivered and waiting for the money to be final
    withdrawn = db.session.query(func.sum(PartnerWithdrawal.amount)).filter(
        PartnerWithdrawal.partner_id == int(partner_id),
        PartnerWithdrawal.status.in_(["PENDING", "PROCESSING", "COMPLETED"])).scalar() or Decimal("0")
    withdrawn = Decimal(str(withdrawn))
    return {"earned": earned, "pending": pending, "withdrawn": withdrawn,
            "available": max(Decimal("0"), earned - withdrawn), "completed_deliveries": completed}


def assignment_public(a, for_rider=False):
    """What the partner (or rider) dashboard shows for one job."""
    order = a.order
    vendor = order.vendor if order else None
    sf = vendor.storefront if vendor else None
    buyer = order.buyer if order else None
    data = {
        "id": a.id,
        "order_id": a.order_id,
        "status": a.status,
        "status_label": STATUS_LABELS.get(a.status, a.status),
        "provider_type": a.provider_type or "siiqo_partner",
        "vendor_name": (sf.store_name if sf else (vendor.full_name if vendor else "")),
        "vendor_phone": ((sf.phone if sf else None) or (vendor.phone if vendor else "")) or "",
        "pickup_address": (getattr(sf, "address", None) or ", ".join(
            x for x in ((sf.city if sf else None), (sf.state if sf else None)) if x)) if sf else "",
        "delivery_address": (order.delivery_address if order else "") or "",
        "city": (order.delivery_city if order else "") or "",
        "state": (order.delivery_state if order else "") or "",
        "buyer_name": ((order.delivery_name or order.buyer_name or (buyer.full_name if buyer else "")) if order
                       else "") or "",
        "buyer_phone": ((order.delivery_phone or order.buyer_phone or (buyer.phone if buyer else "")) if order
                        else "") or "",
        "payment_method": order.payment_method if order else None,
        "needs_code": True,
        "rider_name": a.rider_name,
        "rider_phone": a.rider_phone,
        "rider_user_id": a.rider_user_id,
        "proof_photo_url": a.proof_photo_url,
        "code_locked": bool(_aware(a.code_locked_until) and _aware(a.code_locked_until) > _now()),
        "created_at": a.created_at.isoformat() if a.created_at else None,
        "assigned_at": a.assigned_at.isoformat() if a.assigned_at else None,
        "picked_up_at": a.picked_up_at.isoformat() if a.picked_up_at else None,
        "delivered_at": a.delivered_at.isoformat() if a.delivered_at else None,
        "date": a.created_at.strftime('%d %b %Y') if a.created_at else "",
    }
    if not for_rider:
        data["fee"] = data["delivery_fee"] = float(a.delivery_fee or 0)
        data["earned"] = _earned(a)
        # Older dashboard field names
        data["vendor"] = data["vendor_name"]
        data["destination"] = data["delivery_address"] or data["city"]
        data["vendor_id"] = vendor.id if vendor else None
    return data
