"""
Disputes and refunds (Siiqo 2.0, Phase 2 item 2-5; master plan section 8.2).

    evidence from both sides -> AI summary for the reviewer -> human decision
    -> deterministic outcome by code -> outcome recorded as trust evidence

Rules this module enforces:

* One active case per order (checked here and by the unique ``open_key``).
* While a case is active the escrow is DISPUTED and ``payout_blocked`` is
  True; every payout path in the codebase checks it (see the payout-path
  table in the 2-5 report).
* The AI only writes a neutral, cited summary for the reviewer. It never
  decides, and it has no access to money. No AI, or any AI failure, means no
  summary: the reviewer reads the raw evidence.
* Only a superadmin decides (the route uses ``_require_superadmin``, which
  enforces 2FA). The decision is audit-logged in the same transaction.
* Money is moved by code:
    - release_to_seller -> ``execute_order_escrow_release`` (existing pipeline);
    - refund_full / refund_partial / split -> the buyer's share is marked
      "refund due - manual" with gateway-specific instructions, because no
      refund API exists in the codebase (Paystack, Flutterwave and Daya
      services only collect, verify and transfer). Nothing pretends a refund
      happened: the escrow becomes REFUNDED only when a superadmin records the
      reference of the refund they sent;
    - the seller's share of a partial/split is a vendor-ledger CREDIT with a
      fixed reference (idempotent), computed with Decimal arithmetic.
* Every outcome writes one ``trust_evidence`` row (``dispute_resolved``) with
  the outcome and whether the seller was at fault.
"""
from __future__ import annotations

import logging
import os
import re
import uuid
from datetime import datetime, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

from sqlalchemy.exc import IntegrityError

from app.extensions import db
from app.models.communication import Notification
from app.models.dispute import DisputeCase, DisputeEvidence, DisputeOutcome, DisputeStatus
from app.models.escrow import EscrowStatus, EscrowTransaction
from app.services import jobs

log = logging.getLogger(__name__)

CENT = Decimal("0.01")
DISPUTABLE_ESCROW_STATUSES = (EscrowStatus.IN_ESCROW, EscrowStatus.SHIPPED, EscrowStatus.DELIVERED)
MAX_REASON_CHARS = 120
MAX_TEXT_CHARS = 2000
MAX_EVIDENCE_PER_PARTY = 20
SUMMARY_JOB = "dispute_summary"
SUMMARY_ACTION = "dispute_summary"
EVIDENCE_TYPE = "dispute_resolved"


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _aware(value: datetime | None) -> datetime | None:
    if value is not None and value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value


def _days(name: str, default: int) -> int:
    try:
        value = int((os.environ.get(name) or "").strip() or default)
    except ValueError:
        return default
    return max(1, value)


def seller_response_days() -> int:
    """Days the responding party has to answer before the case goes to review."""
    return _days("DISPUTE_SELLER_RESPONSE_DAYS", 3)


def buyer_reply_days() -> int:
    """Days the opening party has to reply to the response before review."""
    return _days("DISPUTE_BUYER_REPLY_DAYS", 3)


class DisputeError(Exception):
    def __init__(self, code: str, message: str, status: int = 400, **extra):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status
        self.extra = extra

    def to_dict(self) -> dict:
        return {"message": self.message, "code": self.code, **self.extra}


# ── Payout guard ────────────────────────────────────────────────────────────
def active_case_for_order(order_id: int) -> DisputeCase | None:
    return (DisputeCase.query
            .filter(DisputeCase.order_id == order_id, DisputeCase.status.in_(DisputeStatus.ACTIVE))
            .first())


def payout_blocked(order=None, escrow=None) -> bool:
    """True when money for this order must not move to the seller: the escrow
    is DISPUTED, or a dispute case for the order is still active. Fails
    closed (True) if the case table cannot be read."""
    if escrow is not None and escrow.status == EscrowStatus.DISPUTED:
        return True
    order_id = getattr(order, "id", None) or getattr(escrow, "order_id", None)
    if not order_id:
        return False
    try:
        return active_case_for_order(order_id) is not None
    except Exception:  # noqa: BLE001 - a guard that cannot read must hold the money
        log.critical("[DISPUTE GUARD] could not read dispute_cases for Order #%s; holding payout", order_id,
                     exc_info=True)
        return True


# ── Money ───────────────────────────────────────────────────────────────────
def _dec(value) -> Decimal:
    try:
        return Decimal(str(value if value is not None else "0")).quantize(CENT, rounding=ROUND_HALF_UP)
    except (InvalidOperation, ValueError):
        raise DisputeError("bad_amount", "Amount must be a number.")


def split_amounts(total, fee, refund) -> dict:
    """Exact split of what the buyer paid (``total``, escrow currency):
    the buyer gets ``refund``; the remainder goes to the seller less Siiqo's
    fee on that remainder (the original fee, pro rata). Always
    refund + seller_net + fee_kept == total."""
    total, fee, refund = _dec(total), _dec(fee), _dec(refund)
    if total <= 0:
        raise DisputeError("bad_amount", "This order has no amount to divide.")
    if refund < 0 or refund > total:
        raise DisputeError("bad_amount", "The refund must be between 0 and the order total.")
    remainder = total - refund
    fee_kept = (fee * remainder / total).quantize(CENT, rounding=ROUND_HALF_UP) if fee > 0 else Decimal("0.00")
    seller_net = remainder - fee_kept
    assert refund + seller_net + fee_kept == total
    return {"total": total, "refund": refund, "seller_net": seller_net, "fee_kept": fee_kept}


def plan_outcome(escrow, outcome: str, refund_amount=None, buyer_share_percent=None) -> dict:
    total = _dec(escrow.amount)
    fee = _dec(escrow.fee_amount or 0)
    if outcome == DisputeOutcome.REFUND_FULL:
        return split_amounts(total, fee, total)
    if outcome == DisputeOutcome.RELEASE_TO_SELLER:
        return split_amounts(total, fee, 0)
    if outcome == DisputeOutcome.REFUND_PARTIAL:
        if refund_amount in (None, ""):
            raise DisputeError("bad_amount", "Enter the amount to return to the buyer.")
        refund = _dec(refund_amount)
        if refund <= 0 or refund >= total:
            raise DisputeError("bad_amount", "A partial refund must be more than 0 and less than the order total.")
        return split_amounts(total, fee, refund)
    if outcome == DisputeOutcome.SPLIT:
        try:
            pct = Decimal(str(buyer_share_percent))
        except (InvalidOperation, ValueError, TypeError):
            raise DisputeError("bad_amount", "Enter the buyer's share as a percentage.")
        if pct <= 0 or pct >= 100:
            raise DisputeError("bad_amount", "The buyer's share must be more than 0% and less than 100%.")
        refund = (total * pct / Decimal(100)).quantize(CENT, rounding=ROUND_HALF_UP)
        return split_amounts(total, fee, refund)
    raise DisputeError("bad_outcome", f"Outcome must be one of {', '.join(DisputeOutcome.ALL)}.")


def gateway_for(order, escrow) -> str:
    from app.services.fx import is_daya_paid, is_flutterwave_paid
    if is_daya_paid(order, escrow):
        return "daya"
    if is_flutterwave_paid(order, escrow):
        return "flutterwave"
    if (order.payment_method or "").upper() in ("PAYSTACK", "ESCROW"):
        return "paystack"
    return "unknown"


def seller_settled_at_checkout(order, escrow) -> bool:
    """True when the gateway already paid the seller at checkout (Paystack
    subaccount split, or a Flutterwave SPLIT: reference), the same test
    ``execute_order_escrow_release`` uses. Siiqo then holds no money to pay
    or withhold."""
    gateway = gateway_for(order, escrow)
    if gateway == "daya":
        return False
    if gateway == "flutterwave":
        return bool(escrow.payscrow_ref and escrow.payscrow_ref.startswith("SPLIT:"))
    try:
        from app.models.user import Storefront
        from app.models.withdrawal import VendorBankAccount
        acc = (VendorBankAccount.query.filter_by(vendor_id=order.vendor_id, is_default=True).first()
               or VendorBankAccount.query.filter_by(vendor_id=order.vendor_id).first())
        if acc and acc.paystack_subaccount_code:
            return True
        sf = Storefront.query.filter_by(vendor_id=order.vendor_id).first()
        return bool(sf and sf.paystack_subaccount_code)
    except Exception:  # noqa: BLE001
        return False


def refund_instructions(order, escrow, amount: Decimal, currency: str) -> str:
    """No refund API exists in Siiqo's code for any gateway, so every refund
    is sent by a person. These are the steps, by the gateway the buyer used."""
    gateway = gateway_for(order, escrow)
    money = f"{currency} {amount:,.2f}"
    ref = escrow.payscrow_transaction_id or escrow.transaction_number
    if gateway == "paystack":
        steps = (f"Paystack dashboard > Transactions > search reference {ref} > Refund. "
                 f"Refund {money}" + (" (partial refund)." if amount < _dec(escrow.amount) else "."))
    elif gateway == "flutterwave":
        steps = (f"Flutterwave dashboard > Transactions > transaction ID {escrow.payscrow_transaction_id or ref} "
                 f"(tx_ref {escrow.transaction_number}) > Refund. Refund {money}.")
    elif gateway == "daya":
        steps = (f"Daya (bank transfer / crypto) has no refund in Siiqo's code. Confirm the buyer's "
                 f"bank account or wallet with the buyer directly (Order #{order.id}), then send {money} "
                 f"from Siiqo's Daya or corporate account.")
    else:
        steps = (f"The payment gateway for Order #{order.id} is not recognised (payment method "
                 f"{order.payment_method or 'unknown'}). Return {money} to the buyer by the route they paid.")
    note = ""
    if seller_settled_at_checkout(order, escrow):
        note = (" The seller was paid at checkout by the gateway's split settlement, so Siiqo holds none "
                "of this order's money: recover the refunded amount from the seller.")
    return (f"Refund due - manual. {steps}{note} Then record the gateway's refund reference here. "
            "Do not record it until the money has been sent.")


# ── Notifications ───────────────────────────────────────────────────────────
def _notify(user_id, title: str, message: str, order_id: int) -> None:
    if user_id:
        db.session.add(Notification(user_id=user_id, title=title, message=message, type="ESCROW",
                                    order_id=order_id))


def _notify_both(case: DisputeCase, title: str, message: str) -> None:
    _notify(case.buyer_id, title, message, case.order_id)
    _notify(case.vendor_id, title, message, case.order_id)


# ── Opening a case ──────────────────────────────────────────────────────────
def party_of(case_or_order, user_id) -> str | None:
    try:
        uid = int(user_id)
    except (TypeError, ValueError):
        return None
    buyer_id = getattr(case_or_order, "buyer_id", None)
    vendor_id = getattr(case_or_order, "vendor_id", None)
    if buyer_id and uid == buyer_id:
        return "buyer"
    if vendor_id and uid == vendor_id:
        return "seller"
    return None


def _new_reference() -> str:
    return f"DSP-{uuid.uuid4().hex[:8].upper()}"


def _timeline_items(order, escrow) -> list[DisputeEvidence]:
    """Order facts from Siiqo's own records, as citable system evidence."""
    from app.models.escrow import DeliveryConfirmationCode, LogisticsAssignment
    from app.models.foundations import OrderStatusHistory

    currency = (escrow.currency or order.currency or "NGN").upper()
    items: list[tuple[datetime | None, str]] = []
    names = ", ".join(
        f"{(it.product.name if it.product else 'Item')} x{it.quantity}" for it in (order.items or [])
    )
    if order.payment_link_id and not names:
        link = getattr(order, "payment_link", None)
        names = getattr(link, "title", None) or "Pay Link item"
    items.append((order.created_at, f"Order #{order.id} placed for {currency} {_dec(escrow.amount):,.2f}"
                  + (f": {names}." if names else ".")))
    if escrow.paid_at:
        items.append((escrow.paid_at, f"Payment received and held by Siiqo ({(order.payment_method or 'unknown').upper()})."))
    for h in (OrderStatusHistory.query.filter_by(order_id=order.id)
              .order_by(OrderStatusHistory.created_at, OrderStatusHistory.id).all()):
        if (h.to_status or "").upper() in ("PROCESSING", "SHIPPED", "DELIVERED", "PENDING_DELIVERY"):
            items.append((h.created_at, f"Order status changed to {h.to_status}."))
    if order.tracking_number:
        items.append((None, f"The seller added tracking number {order.tracking_number}."))
    assignment = LogisticsAssignment.query.filter_by(order_id=order.id).first()
    if assignment and assignment.delivered_at:
        items.append((assignment.delivered_at, "The delivery partner marked the order delivered."))
    code = db.session.get(DeliveryConfirmationCode, order.id)
    if code and code.sent_at:
        items.append((code.sent_at, "A delivery confirmation code was emailed to the buyer."))
    if escrow.status == EscrowStatus.DELIVERED or (order.status or "").upper() == "DELIVERED":
        items.append((None, "At the time of the dispute the order was marked delivered; the buyer had not confirmed receipt."))
    return [DisputeEvidence(party="system", kind="timeline", text=text, occurred_at=when)
            for when, text in items]


def open_case(order, user_id, reason: str, details: str | None = None, *, photo_url: str | None = None,
              allow_seller: bool = False) -> DisputeCase:
    """Open a case and freeze the payout. Commits."""
    party = party_of(order, user_id)
    if party is None or (party == "seller" and not allow_seller):
        raise DisputeError("forbidden", "Only the buyer on this order can raise a dispute.", 403)
    reason = (reason or "").strip()
    details = (details or "").strip()
    if not reason:
        raise DisputeError("reason_required", "Choose a reason for the dispute.")
    if len(reason) > MAX_REASON_CHARS:
        reason = reason[:MAX_REASON_CHARS]
    if len(details) > MAX_TEXT_CHARS:
        raise DisputeError("too_long", f"Keep the details under {MAX_TEXT_CHARS} characters.")

    escrow = (db.session.query(EscrowTransaction).filter_by(order_id=order.id).with_for_update().first())
    if escrow is None:
        raise DisputeError("no_escrow", "This order has no Siiqo escrow payment to dispute.", 404)
    existing = active_case_for_order(order.id)
    if existing is not None or escrow.status == EscrowStatus.DISPUTED:
        raise DisputeError("already_open", "A dispute is already open on this order.", 409,
                           dispute_reference=existing.reference if existing else escrow.dispute_id)
    if escrow.status not in DISPUTABLE_ESCROW_STATUSES:
        raise DisputeError("not_disputable", f"A dispute can't be raised on an order whose payment is {escrow.status}.")

    respondent = "seller" if party == "buyer" else "buyer"
    now = utcnow()
    case = DisputeCase(
        reference=_new_reference(), order_id=order.id, escrow_id=escrow.id,
        buyer_id=order.buyer_id, vendor_id=order.vendor_id, open_key=str(order.id),
        raised_by=party, reason=reason, pre_dispute_escrow_status=escrow.status,
        status=DisputeStatus.AWAITING_SELLER if respondent == "seller" else DisputeStatus.AWAITING_BUYER,
        deadline_at=now + timedelta(days=seller_response_days()),
        currency=(escrow.currency or "NGN").upper(),
    )
    case.evidence.extend(_timeline_items(order, escrow))
    statement = reason + (f": {details}" if details else "")
    case.evidence.append(DisputeEvidence(party=party, author_user_id=int(user_id), kind="text", text=statement))
    if photo_url:
        case.evidence.append(DisputeEvidence(party=party, author_user_id=int(user_id), kind="photo",
                                             file_url=photo_url))
    db.session.add(case)

    escrow.status = EscrowStatus.DISPUTED
    escrow.dispute_id = case.reference
    escrow.dispute_reason = statement[:2000]
    order.status = "DISPUTED"
    days = seller_response_days()
    _notify(order.buyer_id, "Dispute opened",
            f"Dispute {case.reference} on Order #{order.id} is open. The payment is on hold. "
            f"The {respondent} has {days} days to respond.", order.id)
    _notify(order.vendor_id, "Dispute opened",
            f"Dispute {case.reference} on Order #{order.id} is open and the payment is on hold. "
            + (f"Please respond with your evidence within {days} days, or the case goes to Siiqo for review without it."
               if respondent == "seller" else "The buyer has been asked to respond."), order.id)
    try:
        db.session.commit()
    except IntegrityError:
        db.session.rollback()
        raise DisputeError("already_open", "A dispute is already open on this order.", 409)
    log.info("[DISPUTE] %s opened on Order #%s by %s", case.reference, order.id, party)

    try:  # existing behaviour of the old dispute endpoint
        from app.services.trust import recalculate_vendor_trust
        recalculate_vendor_trust(order.vendor_id, reason="Dispute Raised")
    except Exception as e:  # noqa: BLE001
        log.error("[TRUST ERROR] Failed to recalculate trust on dispute raise: %s", e)
    return case


# ── Evidence and responses ──────────────────────────────────────────────────
def _require_party(case: DisputeCase, user_id) -> str:
    party = party_of(case, user_id)
    if party is None:
        raise DisputeError("forbidden", "You are not a party to this dispute.", 403)
    return party


def add_evidence(case: DisputeCase, user_id, *, text: str | None = None, file_url: str | None = None) -> list:
    """Add a text and/or photo item. Only while the parties are still answering. Commits."""
    party = _require_party(case, user_id)
    if case.status not in (DisputeStatus.OPEN, DisputeStatus.AWAITING_SELLER, DisputeStatus.AWAITING_BUYER):
        raise DisputeError("closed_for_evidence", "This dispute is with the Siiqo team; evidence is closed.", 409)
    text = (text or "").strip()
    if not text and not file_url:
        raise DisputeError("empty", "Add a message or a photo.")
    if len(text) > MAX_TEXT_CHARS:
        raise DisputeError("too_long", f"Keep each message under {MAX_TEXT_CHARS} characters.")
    count = DisputeEvidence.query.filter_by(case_id=case.id, party=party).count()
    adding = int(bool(text)) + int(bool(file_url))
    if count + adding > MAX_EVIDENCE_PER_PARTY:
        raise DisputeError("too_many", f"Each side can add up to {MAX_EVIDENCE_PER_PARTY} items.")
    added = []
    if text:
        added.append(DisputeEvidence(case_id=case.id, party=party, author_user_id=int(user_id), kind="text", text=text))
    if file_url:
        added.append(DisputeEvidence(case_id=case.id, party=party, author_user_id=int(user_id), kind="photo",
                                     file_url=file_url))
    db.session.add_all(added)
    other = case.vendor_id if party == "buyer" else case.buyer_id
    _notify(other, "New dispute evidence", f"The {party} added evidence to dispute {case.reference}.", case.order_id)
    db.session.commit()
    return added


def submit_response(case: DisputeCase, user_id) -> DisputeCase:
    """The party whose turn it is says they have finished. The respondent's
    answer hands the case back to the opener for one reply; the opener's
    reply sends it to review. Commits."""
    party = _require_party(case, user_id)
    waiting_for = {DisputeStatus.AWAITING_SELLER: "seller", DisputeStatus.AWAITING_BUYER: "buyer"}.get(case.status)
    if waiting_for != party:
        raise DisputeError("not_your_turn", "This dispute is not waiting for your response.", 409)
    if party != case.raised_by:
        case.status = DisputeStatus.AWAITING_BUYER if case.raised_by == "buyer" else DisputeStatus.AWAITING_SELLER
        case.deadline_at = utcnow() + timedelta(days=buyer_reply_days())
        opener = case.buyer_id if case.raised_by == "buyer" else case.vendor_id
        _notify(opener, "The other side responded",
                f"The {party} responded to dispute {case.reference}. You have {buyer_reply_days()} days to reply "
                "with anything else before Siiqo reviews it.", case.order_id)
        db.session.commit()
        return case
    move_to_review(case, "both_answered")
    return case


# ── Review and the summary job ──────────────────────────────────────────────
def move_to_review(case: DisputeCase, reason: str) -> bool:
    """Close evidence and queue the reviewer summary. Commits."""
    if case.status not in (DisputeStatus.OPEN, DisputeStatus.AWAITING_SELLER, DisputeStatus.AWAITING_BUYER):
        return False
    case.status = DisputeStatus.UNDER_REVIEW
    case.review_started_at = utcnow()
    case.review_reason = reason[:64]
    case.deadline_at = None
    _notify_both(case, "Dispute under review",
                 f"Dispute {case.reference} is now with the Siiqo team. We'll let you know the decision.")
    db.session.commit()
    queue_summary(case)
    return True


def queue_summary(case: DisputeCase) -> None:
    """Start a summary for the case's next revision (background job)."""
    case.summary_revision = (case.summary_revision or 0) + 1
    case.summary_status = "pending"
    case.summary = None
    case.summary_error = None
    db.session.commit()
    try:
        job = jobs.enqueue(SUMMARY_JOB, {"case_id": case.id, "revision": case.summary_revision})
        case = db.session.get(DisputeCase, case.id)
        if case.summary_job_id != job.id:
            case.summary_job_id = job.id
            db.session.commit()
    except Exception as exc:  # noqa: BLE001 - no summary is an accepted state
        db.session.rollback()
        log.warning("[DISPUTE] summary job for case %s not queued: %s", case.id, exc)
        case = db.session.get(DisputeCase, case.id)
        case.summary_status = "failed"
        case.summary_error = "could not queue the summary job"[:255]
        db.session.commit()


def sweep_deadlines(now: datetime | None = None) -> int:
    """Cases whose answering window has passed go to review. Idempotent."""
    now = now or utcnow()
    due = (DisputeCase.query
           .filter(DisputeCase.status.in_([DisputeStatus.AWAITING_SELLER, DisputeStatus.AWAITING_BUYER]),
                   DisputeCase.deadline_at.isnot(None), DisputeCase.deadline_at <= now)
           .order_by(DisputeCase.id).all())
    moved = 0
    for case in due:
        who = "seller" if case.status == DisputeStatus.AWAITING_SELLER else "buyer"
        if move_to_review(case, f"{who}_deadline"):
            moved += 1
    return moved


def adopt_legacy_disputes() -> int:
    """Escrows put in DISPUTED by the old endpoint (no case row) become cases
    under review, so the reviewer and the payout guard see them. Idempotent."""
    adopted = 0
    for escrow in EscrowTransaction.query.filter_by(status=EscrowStatus.DISPUTED).all():
        if DisputeCase.query.filter_by(order_id=escrow.order_id).first() is not None:
            continue
        order = escrow.order
        if order is None:
            continue
        case = DisputeCase(
            reference=escrow.dispute_id if (escrow.dispute_id or "").startswith("DSP-") else _new_reference(),
            order_id=order.id, escrow_id=escrow.id, buyer_id=order.buyer_id, vendor_id=order.vendor_id,
            open_key=str(order.id), raised_by="buyer", reason=(escrow.dispute_reason or "Dispute")[:MAX_REASON_CHARS],
            status=DisputeStatus.OPEN, currency=(escrow.currency or "NGN").upper(),
        )
        case.evidence.extend(_timeline_items(order, escrow))
        case.evidence.append(DisputeEvidence(
            party="system", kind="text",
            text=f"Raised before dispute cases existed (ref {escrow.dispute_id or 'none'}). "
                 f"Reason given: {escrow.dispute_reason or 'none recorded'}"))
        db.session.add(case)
        try:
            db.session.commit()
        except IntegrityError:
            db.session.rollback()
            continue
        move_to_review(case, "legacy")
        adopted += 1
    return adopted


def get_ai_client():
    """The Anthropic client handed to the gateway. None = the gateway's
    default. Tests monkeypatch this to return a fake client."""
    return None


SUMMARY_SECTIONS = ("timeline", "buyer", "seller", "disagreements")

# Words that would turn a summary into a recommendation (spec: no "winner"
# or "refund" words). "faulty" (a product fault) stays allowed; "fault" does not.
_BANNED_RE = re.compile(
    r"\b(win|wins|winner|winners|winning|lose|loses|loser|losers|refund\w*|releas\w*|recommend\w*|"
    r"should|ought|favou?r\w*|faults?|blame\w*|liable|liability|guilty|deserv\w*|verdict|award\w*)\b",
    re.IGNORECASE,
)
_SENTENCE_END_RE = re.compile(r"[.!?](?=\s+[A-Z0-9\"'(])")


def summary_inputs(case: DisputeCase) -> tuple[dict, set]:
    order = case.order
    evidence = []
    for e in case.evidence:
        when = e.occurred_at or e.created_at
        item = {"id": e.cite_id, "party": e.party, "kind": e.kind,
                "at": when.isoformat() if when else None}
        if e.kind == "photo":
            item["text"] = "Photo attached (not described)."
        else:
            item["text"] = (e.text or "")[:MAX_TEXT_CHARS]
        evidence.append(item)
    inputs = {
        "case": {
            "reference": case.reference,
            "raised_by": case.raised_by,
            "reason": case.reason,
            "order_total": f"{case.currency or 'NGN'} {_dec(order.escrow.amount if order and order.escrow else 0):,.2f}",
            "seller_responded": any(e.party == "seller" for e in case.evidence),
            "moved_to_review_because": case.review_reason,
        },
        "evidence": evidence,
    }
    return inputs, {e.cite_id for e in case.evidence}


def critique(data, known_ids: set) -> list[str]:
    """Code critic for the AI summary. Every sentence cites known evidence,
    is one sentence, and uses none of the recommendation words."""
    if not isinstance(data, dict):
        return ["summary is not an object"]
    problems: list[str] = []
    count = 0
    for section in SUMMARY_SECTIONS:
        items = data.get(section)
        if not isinstance(items, list):
            problems.append(f"{section}: missing")
            continue
        for i, item in enumerate(items):
            where = f"{section}[{i}]"
            text = (item.get("text") if isinstance(item, dict) else "") or ""
            cites = (item.get("cites") if isinstance(item, dict) else None) or []
            if not text.strip():
                problems.append(f"{where}: empty sentence")
                continue
            count += 1
            if _SENTENCE_END_RE.search(text.strip()):
                problems.append(f"{where}: more than one sentence")
            if not isinstance(cites, list) or not cites:
                problems.append(f"{where}: no evidence cited")
            else:
                unknown = [c for c in cites if c not in known_ids]
                if unknown:
                    problems.append(f"{where}: cites unknown evidence {unknown}")
            banned = _BANNED_RE.search(text)
            if banned:
                problems.append(f"{where}: uses the word {banned.group(0)!r}")
    if count == 0:
        problems.append("summary is empty")
    return problems


@jobs.handler(SUMMARY_JOB)
def run_summary_job(payload: dict, job) -> dict:
    """Background job: one AI call (plus one retry) and the code critic.
    Any failure leaves no summary; the reviewer reads the raw evidence."""
    from app.services.ai import AICapExceeded, AIDisabled, AIError, AIUnavailable, run as ai_run

    case = db.session.get(DisputeCase, int(payload.get("case_id") or 0))
    if case is None:
        return {"skipped": "no case"}
    if case.status != DisputeStatus.UNDER_REVIEW or case.summary_revision != payload.get("revision"):
        return {"skipped": "case moved on"}
    inputs, known = summary_inputs(case)
    case_id = case.id
    db.session.commit()   # the gateway must be called with a clean session

    def _finish(status: str, summary=None, error: str | None = None) -> dict:
        c = db.session.get(DisputeCase, case_id)
        if c.summary_revision != payload.get("revision"):
            return {"skipped": "case moved on"}
        c.summary_status = status
        c.summary = summary
        c.summary_error = (error or None) and error[:255]
        db.session.commit()
        return {"status": status, "error": error}

    last_error = None
    for attempt in range(2):          # loop limit: 1 retry
        try:
            result = ai_run(SUMMARY_ACTION, None, inputs, client=get_ai_client())
        except (AIDisabled, AICapExceeded) as exc:
            return _finish("unavailable", error=f"AI off: {exc.reason or exc}")
        except AIUnavailable as exc:
            if exc.reason in ("no_api_key", "sdk_missing"):
                return _finish("unavailable", error=f"AI off: {exc.reason}")
            last_error = f"AI unavailable: {exc.reason or exc}"
            continue
        except AIError as exc:
            last_error = f"{type(exc).__name__}: {getattr(exc, 'reason', None) or exc}"
            continue
        problems = critique(result.data, known)
        if not problems:
            summary = {
                **{s: result.data.get(s) or [] for s in SUMMARY_SECTIONS},
                "prompt_version": result.prompt_version,
                "model": result.served_model,
                "revision": payload.get("revision"),
                "generated_at": utcnow().isoformat(),
                "attempts": attempt + 1,
            }
            return _finish("ready", summary=summary)
        last_error = "critic rejected: " + "; ".join(problems[:4])
        log.info("[DISPUTE] summary for case %s rejected by critic (attempt %s): %s", case_id, attempt + 1, problems)
    return _finish("failed", error=last_error)


# ── Decision and execution ─────────────────────────────────────────────────
def _record_evidence(case: DisputeCase) -> None:
    """One trust_evidence row per outcome (idempotent per case)."""
    from app.models.telemetry import TrustEvidence
    exists = TrustEvidence.query.filter_by(evidence_type=EVIDENCE_TYPE, source="dispute_cases",
                                           source_id=str(case.id)).first()
    if exists:
        return
    db.session.add(TrustEvidence(
        business_id=case.vendor_id,
        evidence_type=EVIDENCE_TYPE,
        provenance="siiqo_transaction_verified",
        source="dispute_cases",
        source_id=str(case.id),
        status="ACTIVE",
        confidence=1,
        properties={
            "order_id": case.order_id,
            "reference": case.reference,
            "outcome": case.outcome,
            "seller_fault": bool(case.seller_fault),
            "refund_amount": str(case.refund_amount) if case.refund_amount is not None else None,
            "seller_amount": str(case.seller_amount) if case.seller_amount is not None else None,
            "currency": case.currency,
            "raised_by": case.raised_by,
        },
        created_at=utcnow(),
        verified_at=utcnow(),
    ))


def _audit(admin, action: str, case: DisputeCase, details: dict, ip: str | None) -> None:
    """Audit row in the caller's transaction (AdminAuditLog.log_action commits on its own)."""
    from flask import has_request_context, request
    from app.models.audit import AdminAuditLog
    db.session.add(AdminAuditLog(
        admin_id=admin.id, admin_email=admin.email, admin_role=admin.role, action=action,
        resource_type="DisputeCase", resource_id=str(case.id), details=details,
        ip_address=(ip or "unknown")[:45],
        user_agent=request.headers.get("User-Agent", "unknown") if has_request_context() else "system",
    ))


def decide(case_id: int, admin, outcome: str, *, seller_fault, note: str | None = None,
           refund_amount=None, buyer_share_percent=None, ip: str | None = None) -> tuple[DisputeCase, bool]:
    """Record a superadmin's decision and execute it. Returns (case, already_done).
    Repeating the same decision is a no-op; a different one is refused."""
    from app.routes.escrow import _credit_vendor_ledger, execute_order_escrow_release

    case = db.session.query(DisputeCase).filter_by(id=case_id).with_for_update().first()
    if case is None:
        raise DisputeError("not_found", "Dispute not found.", 404)
    if case.status == DisputeStatus.RESOLVED:
        if case.outcome == outcome:
            return case, True
        raise DisputeError("already_resolved", f"This dispute was already decided ({case.outcome}).", 409)
    if case.status != DisputeStatus.UNDER_REVIEW:
        raise DisputeError("not_under_review", "Move the dispute to review before deciding it.", 409)
    if outcome not in DisputeOutcome.ALL:
        raise DisputeError("bad_outcome", f"Outcome must be one of {', '.join(DisputeOutcome.ALL)}.")
    if not isinstance(seller_fault, bool):
        raise DisputeError("seller_fault_required", "Say whether the seller was at fault (true or false).")
    order = case.order
    escrow = db.session.query(EscrowTransaction).filter_by(order_id=case.order_id).with_for_update().first()
    if order is None or escrow is None:
        raise DisputeError("no_escrow", "The order's escrow record is missing.", 409)
    if escrow.status != EscrowStatus.DISPUTED:
        raise DisputeError("escrow_not_disputed",
                           f"The escrow is {escrow.status}, not DISPUTED; nothing was changed.", 409)

    plan = plan_outcome(escrow, outcome, refund_amount, buyer_share_percent)
    currency = (escrow.currency or "NGN").upper()
    settled = seller_settled_at_checkout(order, escrow)
    now = utcnow()

    case.status = DisputeStatus.RESOLVED
    case.open_key = None
    case.outcome = outcome
    case.seller_fault = seller_fault
    case.decision_note = (note or "").strip()[:4000] or None
    case.decided_by_admin_id = admin.id
    case.decided_at = now
    case.resolved_at = now
    case.currency = currency
    case.refund_amount = plan["refund"]
    case.seller_amount = plan["seller_net"]
    case.fee_kept = plan["fee_kept"]

    if plan["refund"] > 0:
        case.refund_status = "manual_due"
        case.refund_instructions = refund_instructions(order, escrow, plan["refund"], currency)
    else:
        case.refund_status = "not_applicable"

    _audit(admin, "DISPUTE_DECIDED", case, {
        "reference": case.reference, "order_id": order.id, "outcome": outcome, "seller_fault": seller_fault,
        "refund_amount": str(plan["refund"]), "seller_amount": str(plan["seller_net"]),
        "fee_kept": str(plan["fee_kept"]), "currency": currency, "note": case.decision_note,
        "seller_settled_at_checkout": settled,
    }, ip)
    _record_evidence(case)

    money = lambda v: f"{currency} {v:,.2f}"  # noqa: E731
    if outcome == DisputeOutcome.RELEASE_TO_SELLER:
        case.seller_payout_status = "released"
        escrow.status = case.pre_dispute_escrow_status or EscrowStatus.IN_ESCROW
        if not execute_order_escrow_release(order, escrow, source=f"dispute-{case.reference}"):
            db.session.rollback()
            raise DisputeError("release_failed", "The release pipeline refused this order; nothing was changed.", 409)
        _notify_both(case, "Dispute decided",
                     f"Dispute {case.reference}: the payment for Order #{order.id} has been released to the seller.")
    elif outcome == DisputeOutcome.REFUND_FULL:
        # The escrow stays DISPUTED (payout blocked) until the refund is
        # recorded as sent; see mark_refund_sent.
        case.seller_payout_status = "already_settled" if settled else "not_applicable"
        if settled:
            case.seller_payout_note = (f"The seller was paid {money(_dec(escrow.amount) - _dec(escrow.fee_amount or 0))} "
                                       "at checkout by the gateway split. Recover it from the seller.")
        _notify(case.buyer_id, "Dispute decided",
                f"Dispute {case.reference}: {money(plan['refund'])} will be returned to you. "
                "We'll tell you when it has been sent.", order.id)
        _notify(case.vendor_id, "Dispute decided",
                f"Dispute {case.reference}: the payment for Order #{order.id} will be returned to the buyer.", order.id)
    else:   # refund_partial | split
        if settled:
            case.seller_payout_status = "already_settled"
            case.seller_payout_note = (
                f"The seller was paid at checkout by the gateway split. Their share under this decision is "
                f"{money(plan['seller_net'])}; recover the difference from the seller.")
        elif currency != "NGN":
            case.seller_payout_status = "manual_due"
            case.seller_payout_note = (f"Order paid in {currency}: pay the seller {money(plan['seller_net'])} "
                                       "manually at the payment-time rate. No ledger credit was written.")
        else:
            _credit_vendor_ledger(
                vendor_id=order.vendor_id, amount=float(plan["seller_net"]),
                reference_id=f"DISPUTE-{case.reference}-SELLER",
                description=f"Dispute {case.reference} share for Order #{order.id}",
            )
            case.seller_payout_status = "credited"
            case.seller_payout_note = (f"{money(plan['seller_net'])} credited to the seller's Siiqo balance "
                                       "(withdrawable).")
        escrow.status = EscrowStatus.RELEASED
        escrow.released_at = now
        order.status = "COMPLETED"
        _notify(case.buyer_id, "Dispute decided",
                f"Dispute {case.reference}: {money(plan['refund'])} will be returned to you. "
                "We'll tell you when it has been sent.", order.id)
        _notify(case.vendor_id, "Dispute decided",
                f"Dispute {case.reference}: {money(plan['refund'])} goes back to the buyer and "
                f"{money(plan['seller_net'])} is your share.", order.id)
    db.session.commit()
    log.warning("[DISPUTE] %s decided %s by admin %s (seller_fault=%s)", case.reference, outcome, admin.id, seller_fault)
    return case, False


def mark_refund_sent(case_id: int, admin, reference: str, ip: str | None = None) -> tuple[DisputeCase, bool]:
    """A superadmin records that the manual refund has been sent. Only then
    does a full-refund escrow become REFUNDED. Idempotent."""
    case = db.session.query(DisputeCase).filter_by(id=case_id).with_for_update().first()
    if case is None:
        raise DisputeError("not_found", "Dispute not found.", 404)
    if case.refund_status == "manual_sent":
        return case, True
    if case.refund_status != "manual_due":
        raise DisputeError("no_refund_due", "No refund is due on this dispute.", 409)
    reference = (reference or "").strip()
    if not reference:
        raise DisputeError("reference_required", "Enter the gateway or bank reference of the refund you sent.")
    case.refund_status = "manual_sent"
    case.refund_reference = reference[:255]
    case.refund_marked_by_admin_id = admin.id
    case.refund_marked_at = utcnow()
    escrow = EscrowTransaction.query.filter_by(order_id=case.order_id).first()
    if case.outcome == DisputeOutcome.REFUND_FULL and escrow is not None and escrow.status == EscrowStatus.DISPUTED:
        # Read by app/services/foundations.py: a refund where the seller was
        # not at fault is not recorded as a lost dispute.
        escrow._dispute_seller_fault = bool(case.seller_fault)
        escrow.status = EscrowStatus.REFUNDED
        if case.order is not None:
            case.order.status = "CANCELLED"
    _audit(admin, "DISPUTE_REFUND_SENT", case, {"reference": case.reference, "order_id": case.order_id,
                                                "refund_amount": str(case.refund_amount),
                                                "currency": case.currency, "refund_reference": case.refund_reference}, ip)
    _notify(case.buyer_id, "Refund sent",
            f"Dispute {case.reference}: {case.currency} {_dec(case.refund_amount):,.2f} has been sent back to you "
            f"(reference {case.refund_reference}).", case.order_id)
    db.session.commit()
    return case, False


# ── Views ───────────────────────────────────────────────────────────────────
def _iso(value):
    value = _aware(value)
    return value.isoformat() if value else None


def _money(value):
    return str(value) if value is not None else None


def party_view(case: DisputeCase, user_id) -> dict:
    party = party_of(case, user_id)
    waiting_for = {DisputeStatus.AWAITING_SELLER: "seller", DisputeStatus.AWAITING_BUYER: "buyer"}.get(case.status)
    data = {
        "id": case.id,
        "reference": case.reference,
        "order_id": case.order_id,
        "status": case.status,
        "reason": case.reason,
        "raised_by": case.raised_by,
        "your_party": party,
        "waiting_for": waiting_for,
        "deadline_at": _iso(case.deadline_at),
        "can_add_evidence": case.status in (DisputeStatus.AWAITING_SELLER, DisputeStatus.AWAITING_BUYER),
        "can_submit": waiting_for is not None and waiting_for == party,
        "evidence": [e.to_dict() for e in case.evidence],
        "created_at": _iso(case.created_at),
    }
    if case.status == DisputeStatus.RESOLVED:
        data["resolution"] = {
            "outcome": case.outcome,
            "currency": case.currency,
            "refund_amount": _money(case.refund_amount),
            "seller_amount": _money(case.seller_amount),
            "refund_status": case.refund_status,
            "decided_at": _iso(case.decided_at),
        }
    return data


def admin_view(case: DisputeCase, *, with_evidence: bool = True) -> dict:
    order = case.order
    escrow = order.escrow if order is not None else None
    data = {
        "id": case.id,
        "reference": case.reference,
        "order_id": case.order_id,
        "buyer_id": case.buyer_id,
        "vendor_id": case.vendor_id,
        "store_name": (order.vendor.storefront.store_name
                       if order is not None and order.vendor is not None and order.vendor.storefront else None),
        "status": case.status,
        "reason": case.reason,
        "raised_by": case.raised_by,
        "deadline_at": _iso(case.deadline_at),
        "review_reason": case.review_reason,
        "review_started_at": _iso(case.review_started_at),
        "created_at": _iso(case.created_at),
        "summary_status": case.summary_status,
        "outcome": case.outcome,
        "seller_fault": case.seller_fault,
        "refund_status": case.refund_status,
    }
    if not with_evidence:
        return data
    money = None
    if escrow is not None:
        total, fee = _dec(escrow.amount), _dec(escrow.fee_amount or 0)
        money = {
            "currency": (escrow.currency or "NGN").upper(),
            "total": str(total),
            "fee": str(fee),
            "seller_net_on_release": str(total - fee),
            "gateway": gateway_for(order, escrow),
            "seller_settled_at_checkout": seller_settled_at_checkout(order, escrow),
            "escrow_status": escrow.status,
            "payment_method": order.payment_method,
        }
    data.update({
        "evidence": [e.to_dict() for e in case.evidence],
        "summary": case.summary if case.summary_status == "ready" else None,
        "summary_error": case.summary_error,
        "money": money,
        "decision": {
            "outcome": case.outcome,
            "seller_fault": case.seller_fault,
            "note": case.decision_note,
            "decided_by_admin_id": case.decided_by_admin_id,
            "decided_at": _iso(case.decided_at),
            "refund_amount": _money(case.refund_amount),
            "seller_amount": _money(case.seller_amount),
            "fee_kept": _money(case.fee_kept),
            "refund_status": case.refund_status,
            "refund_instructions": case.refund_instructions,
            "refund_reference": case.refund_reference,
            "refund_marked_at": _iso(case.refund_marked_at),
            "seller_payout_status": case.seller_payout_status,
            "seller_payout_note": case.seller_payout_note,
        } if case.status == DisputeStatus.RESOLVED else None,
        "can_decide": case.status == DisputeStatus.UNDER_REVIEW,
    })
    return data
