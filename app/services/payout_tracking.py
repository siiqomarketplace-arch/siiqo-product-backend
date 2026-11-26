"""Every payout transfer is followed until the gateway gives its FINAL answer.

A transfer the gateway accepted is not money in the seller's bank. Each one
(order payout at release, seller withdrawal, delivery partner withdrawal) is
stored in ``payout_transfers`` as 'pending' and finished by:

  * the gateway's transfer webhook (signature checked by the existing handler):
      Paystack  transfer.success / transfer.failed / transfer.reversed
                (https://paystack.com/docs/transfers/single-transfers/ ;
                 mirror https://docs-v2.paystack.com/docs/transfers/single-transfers/)
      Flutterwave  "transfer.completed" with data.status SUCCESSFUL / FAILED
                (https://developer.flutterwave.com/docs/webhooks). Its webhook
                is checked only by a shared secret (verif-hash), so a FAILED
                status is confirmed with GET /v3/transfers/{id} before any
                money goes back to a balance.
      Daya      transfer.completed / transfer.failed / transfer.reversed
                (https://docs.daya.co/api-reference/webhooks/events)
  * a scheduled check (run_transfer_check, every 30 min) for transfers still
    pending after PAYOUT_TRANSFER_CHECK_AFTER_HOURS (default 2): Paystack
    GET /transfer/verify/:reference, Flutterwave GET /v3/transfers/{id},
    Daya GET /v1/transfers/{id}.

On failed / reversed: the payout DEBIT is reversed with one CREDIT (the money
is back in the seller's / partner's withdrawable balance), they are told once,
and it is listed in admin "Payouts that need a look". Never re-sent
automatically. Idempotent: a repeat webhook or check changes nothing.
"""
import logging
import os
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from app.extensions import db

log = logging.getLogger(__name__)

PENDING, SUCCESS, FAILED, REVERSED = "pending", "success", "failed", "reversed"
FINAL_BAD = (FAILED, REVERSED)
RETURNED_SUFFIX = "-RETURNED"
ORDER_FAILED_TITLE = "Payout didn't go through"
WITHDRAWAL_FAILED_TITLE = "Withdrawal didn't go through"


def _utcnow():
    return datetime.now(timezone.utc)


def _aware(dt):
    if dt is None:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def check_after_hours() -> float:
    try:
        return max(0.0, float(os.environ.get("PAYOUT_TRANSFER_CHECK_AFTER_HOURS", "2")))
    except ValueError:
        return 2.0


def normalise_status(gateway: str, raw) -> str:
    """Map a gateway's own status word to pending / success / failed / reversed.
    Anything not clearly final stays pending (and is flagged when it is old)."""
    s = str(raw or "").strip().lower()
    if gateway == "paystack":
        return {"success": SUCCESS, "failed": FAILED, "reversed": REVERSED}.get(s, PENDING)
    if gateway == "flutterwave":
        return {"successful": SUCCESS, "success": SUCCESS, "failed": FAILED,
                "reversed": REVERSED}.get(s, PENDING)
    if gateway == "daya":
        return {"settled": SUCCESS, "completed": SUCCESS, "successful": SUCCESS, "success": SUCCESS,
                "failed": FAILED, "reversed": REVERSED}.get(s, PENDING)
    return PENDING


def record_transfer(*, kind: str, user_id: int, gateway: str, reference: str, amount,
                    gateway_transfer_id=None, order_id=None, withdrawal_id=None,
                    partner_withdrawal_id=None, debit_reference=None, gateway_status=None):
    """Store (or refresh) a transfer the gateway accepted. Never commits.
    The same reference (a repeat call for the same order) updates one row."""
    from app.models.payouts import PayoutTransfer
    if not reference:
        return None
    row = PayoutTransfer.query.filter_by(reference=reference).first()
    if row is None:
        row = PayoutTransfer(kind=kind, user_id=int(user_id), gateway=gateway, reference=reference,
                             amount=Decimal(str(amount)).quantize(Decimal("0.01")), status=PENDING)
        db.session.add(row)
    elif row.status in FINAL_BAD and row.restored_at is not None:
        # A transfer that failed and was given back is never resurrected here.
        return row
    row.gateway = gateway
    row.order_id = order_id if order_id is not None else row.order_id
    row.withdrawal_id = withdrawal_id if withdrawal_id is not None else row.withdrawal_id
    row.partner_withdrawal_id = (partner_withdrawal_id if partner_withdrawal_id is not None
                                 else row.partner_withdrawal_id)
    if gateway_transfer_id:
        row.gateway_transfer_id = str(gateway_transfer_id)[:120]
    if debit_reference:
        row.debit_reference = debit_reference
    if gateway_status:
        row.gateway_status = str(gateway_status)[:40]
        if normalise_status(gateway, gateway_status) == SUCCESS and row.status == PENDING:
            row.status = SUCCESS
            row.finalized_at = _utcnow()
    db.session.flush()
    return row


def latest_for_order(order_id):
    from app.models.payouts import PayoutTransfer
    return (PayoutTransfer.query.filter_by(kind="order", order_id=order_id)
            .order_by(PayoutTransfer.id.desc()).first())


def _find(gateway, reference=None, transfer_id=None):
    from app.models.payouts import PayoutTransfer
    row = None
    if reference:
        row = PayoutTransfer.query.filter_by(reference=str(reference)).first()
    if row is None and transfer_id:
        row = (PayoutTransfer.query.filter_by(gateway=gateway, gateway_transfer_id=str(transfer_id))
               .first())
    if row is None:
        return None
    return (db.session.query(type(row)).filter_by(id=row.id)
            .with_for_update().populate_existing().first())


# ── giving the money back ────────────────────────────────────────────────────

def _notify(user_id, title, message, order_id=None, ntype="ESCROW", email_subject=None, cta_url=None):
    from app.models.communication import Notification
    try:
        if order_id is not None and Notification.query.filter_by(
                user_id=user_id, order_id=order_id, title=title, message=message).first():
            return
        db.session.add(Notification(user_id=user_id, title=title, message=message, type=ntype,
                                    order_id=order_id))
        from app.models.user import User
        user = db.session.get(User, user_id)
        if user is not None and user.email:
            try:
                from app.utils.email import send_siiqo_email
                send_siiqo_email(to_email=user.email, subject=email_subject or f"Siiqo - {title}",
                                 template_name="base_notification", first_name=user.first_name or "there",
                                 title=title, message=message, cta_text="Open Siiqo",
                                 cta_url=cta_url or "https://siiqo.com/vendor/escrow")
            except Exception as exc:  # noqa: BLE001
                log.info("[PAYOUT TRACK] email not sent to user %s: %s", user_id, exc)
    except Exception as exc:  # noqa: BLE001
        log.warning("[PAYOUT TRACK] notice not recorded for user %s: %s", user_id, exc)


def _restore(row) -> None:
    """Put the money back where it can be withdrawn again. Exactly once."""
    from app.models.finance import Ledger
    if row.restored_at is not None:
        return
    amount = Decimal(str(row.amount)).quantize(Decimal("0.01"))
    if row.kind == "order":
        from app.routes.escrow import _credit_vendor_ledger
        debit = None
        if row.debit_reference:
            debit = Ledger.query.filter_by(vendor_id=row.user_id, transaction_type="DEBIT",
                                           reference_id=row.debit_reference).first()
        if debit is not None:
            _credit_vendor_ledger(
                vendor_id=row.user_id, amount=float(debit.amount),
                reference_id=f"{row.debit_reference}{RETURNED_SUFFIX}",
                description=f"Payout for Order #{row.order_id} didn't go through; back in your balance",
            )
            amount = Decimal(str(debit.amount)).quantize(Decimal("0.01"))
        # No debit: the payout was never taken out of the balance, so the
        # money is already there; nothing to write.
        _notify(row.user_id, ORDER_FAILED_TITLE,
                f"Your payout for Order #{row.order_id} didn't go through; it's back in your Siiqo balance. "
                "Withdraw it from Payouts.",
                order_id=row.order_id,
                email_subject=f"Your payout for Order #{row.order_id} is back in your Siiqo balance")
    elif row.kind == "withdrawal":
        from app.models.withdrawal import Withdrawal
        from app.routes.withdrawal import _get_ledger_balance
        w = db.session.get(Withdrawal, row.withdrawal_id) if row.withdrawal_id else None
        if w is not None:
            w.status = 'FAILED'
            w.failure_reason = (row.failure_reason or f"Transfer {row.status}")[:500]
        ref = f"{row.reference}-REVERSAL"
        if not Ledger.query.filter_by(vendor_id=row.user_id, transaction_type="CREDIT",
                                      reference_id=ref).first():
            db.session.add(Ledger(
                vendor_id=row.user_id, transaction_type='CREDIT', amount=amount,
                description=f"Reversal of failed withdrawal {row.reference}", reference_id=ref,
                balance_after=Decimal(str(_get_ledger_balance(row.user_id))) + amount,
            ))
        _notify(row.user_id, WITHDRAWAL_FAILED_TITLE,
                f"Your withdrawal of ₦{amount:,.2f} didn't go through; it's back in your Siiqo balance.")
    elif row.kind == "partner_withdrawal":
        from app.models.withdrawal import PartnerWithdrawal
        w = db.session.get(PartnerWithdrawal, row.partner_withdrawal_id) if row.partner_withdrawal_id else None
        if w is not None:
            # Earnings minus non-failed withdrawals (logistics_flow.partner_earnings):
            # FAILED gives the amount back to the partner's balance.
            w.status = 'FAILED'
            w.failure_reason = (row.failure_reason or f"Transfer {row.status}")[:500]
        _notify(row.user_id, WITHDRAWAL_FAILED_TITLE,
                f"Your withdrawal of ₦{amount:,.2f} didn't go through; it's back in your Siiqo earnings.",
                ntype="DELIVERY", cta_url="https://siiqo.com/partners/dashboard/earnings")
    row.restored_at = _utcnow()


def _mark_success(row) -> None:
    if row.kind == "withdrawal" and row.withdrawal_id:
        from app.models.withdrawal import Withdrawal
        w = db.session.get(Withdrawal, row.withdrawal_id)
        if w is not None and w.status in ("PENDING", "PROCESSING", "SENT"):
            w.status = "COMPLETED"
    elif row.kind == "partner_withdrawal" and row.partner_withdrawal_id:
        from app.models.withdrawal import PartnerWithdrawal
        w = db.session.get(PartnerWithdrawal, row.partner_withdrawal_id)
        if w is not None and w.status in ("PENDING", "PROCESSING"):
            w.status = "COMPLETED"


def apply_outcome(gateway: str, *, reference=None, transfer_id=None, raw_status=None,
                  reason=None, source="webhook") -> dict:
    """Apply a gateway's answer about one transfer. Commits. Idempotent.
    Returns {"found": bool, "status": ..., "changed": bool}."""
    row = _find(gateway, reference, transfer_id)
    if row is None:
        return {"found": False, "changed": False}
    new = normalise_status(gateway, raw_status)
    row.gateway_status = str(raw_status or "")[:40] or row.gateway_status
    row.last_checked_at = _utcnow()
    changed = False
    if new == SUCCESS and row.status == PENDING:
        row.status, row.finalized_at, changed = SUCCESS, _utcnow(), True
        _mark_success(row)
    elif new in FINAL_BAD and (row.status == PENDING or (row.status == SUCCESS and new == REVERSED)):
        row.status, row.finalized_at, changed = new, _utcnow(), True
        if reason:
            row.failure_reason = str(reason)[:500]
        _restore(row)
        log.error("[PAYOUT TRACK] %s transfer %s %s (%s, %s): ₦%s back in user %s's balance",
                  gateway, row.reference, new, row.kind, source, row.amount, row.user_id)
    db.session.commit()
    return {"found": True, "status": row.status, "changed": changed}


# ── scheduled check ──────────────────────────────────────────────────────────

def _ask_gateway(row) -> dict:
    """{'ok': bool, 'status': raw, 'reason': str}"""
    if row.gateway == "paystack":
        from app.services.escrow.paystack_provider import paystack_verify_transfer
        res = paystack_verify_transfer(row.reference)
    elif row.gateway == "flutterwave":
        if not row.gateway_transfer_id:
            return {"ok": False}
        from app.services import flutterwave_service
        res = flutterwave_service.get_transfer_status(row.gateway_transfer_id)
        if res.get("success"):
            res["reason"] = (res.get("data") or {}).get("complete_message")
    elif row.gateway == "daya":
        if not row.gateway_transfer_id:
            return {"ok": False}
        from app.services import daya_service
        res = daya_service.get_transfer(row.gateway_transfer_id)
    else:
        return {"ok": False}
    if not res.get("success"):
        return {"ok": False}
    return {"ok": True, "status": res.get("status"), "reason": res.get("reason")}


def confirm_flutterwave_failure(transfer_id) -> bool:
    """Flutterwave's webhook is only protected by a shared secret: before
    money goes back to a balance, Flutterwave's API must say FAILED too."""
    if not transfer_id:
        return False
    from app.services import flutterwave_service
    res = flutterwave_service.get_transfer_status(transfer_id)
    return bool(res.get("success")) and normalise_status("flutterwave", res.get("status")) in FINAL_BAD


def run_transfer_check(now=None, limit: int = 50) -> dict:
    """Every 30 minutes: ask the gateway about transfers still pending after
    PAYOUT_TRANSFER_CHECK_AFTER_HOURS. Bounded; oldest first."""
    from app.models.payouts import PayoutTransfer
    now = now or _utcnow()
    cutoff = now - timedelta(hours=check_after_hours())
    rows = (PayoutTransfer.query.filter(PayoutTransfer.status == PENDING, PayoutTransfer.created_at <= cutoff)
            .order_by(PayoutTransfer.last_checked_at.asc().nullsfirst(), PayoutTransfer.id.asc())
            .limit(limit).all())
    done = {"checked": 0, "success": 0, "failed": 0, "still_pending": 0, "unreachable": 0}
    for row in rows:
        try:
            ans = _ask_gateway(row)
        except Exception as exc:  # noqa: BLE001
            log.warning("[PAYOUT TRACK] check of %s failed: %s", row.reference, exc)
            ans = {"ok": False}
        done["checked"] += 1
        if not ans.get("ok"):
            row.last_checked_at = now
            row.check_count = (row.check_count or 0) + 1
            db.session.commit()
            done["unreachable"] += 1
            continue
        res = apply_outcome(row.gateway, reference=row.reference, raw_status=ans.get("status"),
                            reason=ans.get("reason"), source="scheduled check")
        row = _find(row.gateway, row.reference)
        if row is not None:
            row.check_count = (row.check_count or 0) + 1
            db.session.commit()
        st = res.get("status")
        if st == SUCCESS:
            done["success"] += 1
        elif st in FINAL_BAD:
            done["failed"] += 1
        else:
            done["still_pending"] += 1
    if done["checked"]:
        log.info("[PAYOUT TRACK] scheduled check: %s", done)
    return done


def needs_a_look(since=None, now=None, limit=200, stuck_hours=24) -> list:
    """Transfers that failed / were reversed, or are still pending after
    `stuck_hours`: for admin "Payouts that need a look"."""
    from sqlalchemy import and_, or_
    from app.models.payouts import PayoutTransfer
    now = now or _utcnow()
    since = since or (now - timedelta(days=60))
    stuck = now - timedelta(hours=stuck_hours)
    rows = (PayoutTransfer.query.filter(
        PayoutTransfer.created_at >= since,
        or_(PayoutTransfer.status.in_(FINAL_BAD),
            and_(PayoutTransfer.status == PENDING, PayoutTransfer.created_at < stuck)))
        .order_by(PayoutTransfer.created_at.desc()).limit(limit).all())
    out = []
    for r in rows:
        what = {"order": f"Order #{r.order_id} payout", "withdrawal": "Seller withdrawal",
                "partner_withdrawal": "Partner withdrawal"}.get(r.kind, r.kind)
        if r.status == PENDING:
            reason = f"{what}: {r.gateway} transfer {r.reference} still pending after {stuck_hours}h"
        else:
            reason = (f"{what}: {r.gateway} transfer {r.reference} {r.status}"
                      + ("; money is back in the balance" if r.restored_at else ""))
        out.append({"kind": "payout_transfer", "status": r.status, "order_id": r.order_id,
                    "vendor_id": r.user_id, "amount_ngn": float(r.amount or 0),
                    "at": _aware(r.finalized_at or r.created_at), "reason": reason,
                    "reference": r.reference, "gateway": r.gateway})
    return out
