"""Paying delivery partners from where the money is.

Buyers of physical goods pay by Daya (bank transfer / crypto), so the delivery
fees partners earn sit in Siiqo's Daya balance. A partner withdrawal is sent:

  1. by Daya NGN bank transfer (daya_service.transfer_ngn_to_vendor), when
     DAYA_API_KEY is set;
  2. by Paystack, only when Daya is not configured or Daya gave a definite
     refusal (HTTP 4xx: nothing was sent). A timeout, connection error, 5xx or
     duplicate answer is an unknown outcome: the withdrawal stays PROCESSING
     (its amount reserved) for an admin to check, and Paystack is NOT tried.
     So one withdrawal is never sent by both.

Money only goes to an account whose holder name a bank lookup confirmed
(app/services/payout_account_name.py rules). The partner row is locked while
the balance is checked and the PROCESSING row written, and that row is
committed before any gateway call. One deterministic reference per withdrawal
(app/routes/withdrawal.partner_withdrawal_reference) is used on either rail.

Automatic weekly payouts: run_weekly_partner_payouts (Friday 09:00 Lagos),
behind PARTNER_AUTO_PAYOUT_ENABLED (default off). One run per ISO week
(partner_payout_runs.run_key unique), same withdrawal function.
"""
from __future__ import annotations

import logging
import os
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from app.extensions import db
from app.utils.log_mask import mask_account

log = logging.getLogger(__name__)

LAGOS = timezone(timedelta(hours=1))   # West Africa Time, no daylight saving
NO_ACCOUNT_MESSAGE = "Add and verify your bank account in Earnings before you withdraw."
NAME_PENDING_MESSAGE = ("We couldn't confirm your bank account name with the bank. Check your bank details "
                        "in Earnings, then try again.")


def _utcnow():
    return datetime.now(timezone.utc)


def auto_payout_enabled() -> bool:
    return (os.environ.get("PARTNER_AUTO_PAYOUT_ENABLED") or "").strip().lower() in ("1", "true", "yes", "on")


def daya_configured() -> bool:
    from app.services import daya_service
    return bool(daya_service._key())


def _default_account(partner_id):
    from app.models.withdrawal import PartnerBankAccount
    return (PartnerBankAccount.query.filter_by(partner_id=partner_id, is_default=True).first()
            or PartnerBankAccount.query.filter_by(partner_id=partner_id).first())


def _confirm_name(acc) -> None:
    """One bank lookup (Daya, then Paystack) for an account whose name is not
    confirmed. Only a name the bank returned is saved. Does not commit."""
    from app.services.payout_account_name import resolve_holder_name
    try:
        res = resolve_holder_name(acc.bank_code, acc.account_number, acc.bank_name)
    except Exception as exc:  # noqa: BLE001
        log.warning("[PARTNER PAYOUT] Name lookup failed for %s: %s", mask_account(acc.account_number),
                    type(exc).__name__)
        return
    if res["status"] == "resolved":
        acc.account_name = res["account_name"]
        acc.is_verified = True
        acc.verified_at = _utcnow()


def _fund_daya_withdrawal_balance(amount_ngn: float, reference: str) -> None:
    """Best effort, as the seller Daya payout does: move enough USD from
    Siiqo's Daya collection balance to its withdrawal balance. Moves money
    only inside Siiqo's own Daya account; a failure here is logged and the
    transfer is attempted anyway (Daya refuses it if the balance is short)."""
    from app.services import daya_service
    try:
        rate = 1500.0
        try:
            rate_data = daya_service.get_rate(asset="USDT", side="SELL")
            if rate_data and rate_data.get("rate"):
                rate = float(rate_data["rate"])
        except Exception:  # noqa: BLE001
            pass
        bal = (daya_service.get_merchant_balance() or {}).get("data", {}) or {}
        collection = float(bal.get("collection_balance_usd", 0) or 0)
        withdrawal = float(bal.get("withdrawal_balance_usd", 0) or 0)
        needed = round((amount_ngn / rate) * 1.02, 4)
        if withdrawal < needed:
            move = min(round(needed - withdrawal + 0.10, 4), collection)
            if move > 0:
                daya_service.transfer_collection_to_withdrawal(amount_usd=move,
                                                               idempotency_key=f"bal-transfer-{reference}")
    except Exception as exc:  # noqa: BLE001
        log.warning("[PARTNER PAYOUT] Daya balance check/move failed for %s: %s", reference, exc)


def send_partner_withdrawal(partner, amount=None, *, source: str = "manual", run_key: str | None = None):
    """Withdraw a partner's earned delivery fees to its bank. Returns
    (http_status, body). Used by the partner's Withdraw button and the weekly
    automatic payout."""
    from app.models.user import User
    from app.models.withdrawal import PartnerWithdrawal
    from app.routes.withdrawal import min_withdrawal_ngn, partner_withdrawal_reference
    from app.services.logistics_flow import partner_earnings
    from app.services.payout_account_name import name_confirmed

    partner_id = int(partner.id)

    # One bank lookup before the lock when the saved name isn't confirmed.
    pre = _default_account(partner_id)
    if pre is not None and pre.account_number and pre.bank_code and not name_confirmed(pre):
        _confirm_name(pre)
        db.session.commit()

    # Serialise withdrawals per partner: hold a row lock on the partner until
    # the withdrawal row is committed, so two requests can't both spend the
    # same balance (the second waits and then sees the first one's row).
    db.session.query(User).filter_by(id=partner_id).with_for_update().first()

    available = partner_earnings(partner_id)['available']
    minimum = Decimal(str(min_withdrawal_ngn()))  # same minimum as sellers (MIN_WITHDRAWAL_NGN)
    if available < minimum:
        db.session.rollback()
        return 400, {'code': 'BELOW_MINIMUM',
                     'message': f'Insufficient balance. Minimum withdrawal is ₦{minimum:,.2f}. '
                                f'Your available balance is ₦{available:,.2f}.'}

    use_daya = daya_configured()
    bank_acc = _default_account(partner_id)
    if not bank_acc or not bank_acc.account_number or not bank_acc.bank_code:
        db.session.rollback()
        return 400, {'code': 'NO_PAYOUT_ACCOUNT', 'message': NO_ACCOUNT_MESSAGE}
    if not name_confirmed(bank_acc):
        # Re-checked under the lock, no network: never send with an unconfirmed name.
        db.session.rollback()
        return 400, {'code': 'ACCOUNT_NAME_NOT_CONFIRMED', 'message': NAME_PENDING_MESSAGE}
    if not use_daya and not bank_acc.recipient_code:
        db.session.rollback()
        return 400, {'code': 'NO_PAYOUT_ACCOUNT', 'message': NO_ACCOUNT_MESSAGE}

    if amount not in (None, ''):
        try:
            requested = Decimal(str(amount)).quantize(Decimal('0.01'))
        except Exception:  # noqa: BLE001
            db.session.rollback()
            return 400, {'message': 'Invalid amount format.'}
        if requested < minimum or requested > available:
            db.session.rollback()
            return 400, {'message': 'Invalid withdrawal amount.'}
    else:
        requested = available  # withdraw all

    # Record (and reserve) the withdrawal before calling any gateway.
    w = PartnerWithdrawal(
        partner_id=partner_id, amount=requested, fee_amount=Decimal('0'), net_amount=requested,
        status='PROCESSING', bank_account_id=bank_acc.id, source=source, run_key=run_key,
    )
    db.session.add(w)
    db.session.flush()
    reference = partner_withdrawal_reference(w)
    w.transfer_reference = reference
    w.withdrawal_number = reference
    db.session.commit()   # releases the lock; the balance is now reserved

    sent = False
    daya_refusal = None
    if use_daya:
        from app.services import daya_service
        _fund_daya_withdrawal_balance(float(requested), reference)
        try:
            res = daya_service.transfer_ngn_to_vendor(
                amount_ngn=float(requested), bank_code=bank_acc.bank_code,
                account_number=bank_acc.account_number, reference=reference, order_id=0,
                account_name=bank_acc.account_name or "",
            )
        except Exception as exc:  # noqa: BLE001 - unknown outcome
            res = {"success": False, "outcome": "unknown", "error_message": str(exc)}
        if res.get("success"):
            w.channel = 'daya'
            w.transfer_code = str(res.get("transfer_id") or "")[:100]
            sent = True
        elif res.get("outcome") in ("refused", "not_configured"):
            daya_refusal = str(res.get("error_message") or "Daya refused the transfer")[:300]
            log.warning("[PARTNER PAYOUT] Daya refused %s; trying Paystack", reference)
        else:
            # The money may have left: keep it reserved and never try Paystack.
            w.channel = 'daya'
            w.failure_reason = f"Daya outcome unknown: {res.get('error_message')}"[:500]
            db.session.commit()
            log.error("[PARTNER PAYOUT] Daya outcome unknown for %s; left PROCESSING for review", reference)
            return 202, {'status': 'processing', 'reference': reference, 'amount': str(requested),
                         'message': "We're confirming your withdrawal with the bank. "
                                    "Check back shortly before trying again."}

    if not sent:
        if not bank_acc.recipient_code:
            w.status = 'FAILED'
            w.failure_reason = (daya_refusal or 'No Paystack recipient for this account')[:500]
            db.session.commit()
            return 400, {'message': f"Withdrawal failed: {w.failure_reason}"}
        from app.services.escrow.paystack_provider import paystack_transfer_to_vendor
        try:
            result = paystack_transfer_to_vendor(
                recipient_code=bank_acc.recipient_code, amount_ngn=float(requested),
                reference=reference, reason="Siiqo partner withdrawal",
            )
        except Exception as e:  # noqa: BLE001 - unknown outcome: keep it reserved for review
            log.error("[PARTNER WITHDRAWAL] transfer call failed for %s: %s", reference, e)
            w.channel = 'paystack'
            db.session.commit()
            return 202, {'status': 'processing', 'reference': reference, 'amount': str(requested),
                         'message': "We're confirming your withdrawal with the bank. "
                                    "Check back shortly before trying again."}
        w.channel = 'paystack'
        if not result.get("success"):
            w.status = 'FAILED'
            reason = result.get('error_message') or 'Transfer failed'
            if daya_refusal:
                reason = f"Daya: {daya_refusal}; Paystack: {reason}"
            w.failure_reason = str(reason)[:500]
            db.session.commit()
            return 400, {'message': f"Withdrawal failed: {result.get('error_message', 'Unknown error.')}"}
        w.transfer_code = result.get("transfer_code", "")

    w.status = 'PENDING'
    w.processed_at = _utcnow()
    # Followed to its final status (app/services/payout_tracking.py): a failed
    # or reversed transfer marks the withdrawal FAILED, which puts the amount
    # back in the partner's earnings. Never re-sent automatically.
    try:
        from app.services.payout_tracking import record_transfer
        record_transfer(kind="partner_withdrawal", user_id=partner_id, gateway=w.channel or "paystack",
                        reference=reference, amount=requested, gateway_transfer_id=w.transfer_code or None,
                        partner_withdrawal_id=w.id)
    except Exception as track_err:  # noqa: BLE001
        log.error("[PARTNER PAYOUT] Could not record transfer %s: %s", reference, track_err)
    db.session.commit()
    return 200, {
        'status': 'success',
        'message': 'Withdrawal initiated. Funds will arrive within 24 hours.',
        'transfer_code': w.transfer_code,
        'reference': reference,
        'channel': w.channel,
        'amount': str(requested),
    }


# ── weekly automatic payouts ───────────────────────────────────────────────

def week_key(now=None) -> str:
    local = (now or _utcnow()).astimezone(LAGOS)
    year, week, _ = local.isocalendar()
    return f"{year}-W{week:02d}"


def _notify_partner(user, amount, status_code, body) -> None:
    from app.models.communication import Notification
    if status_code == 200:
        title = "Weekly payout sent"
        msg = (f"We sent your delivery earnings of ₦{Decimal(str(amount)):,.2f} to your bank. "
               "It should arrive within 24 hours.")
    elif status_code == 202:
        title = "Weekly payout being checked"
        msg = (f"Your weekly payout of ₦{Decimal(str(amount)):,.2f} is being confirmed with the bank. "
               "We'll update you if anything is wrong.")
    else:
        title = "Weekly payout didn't go through"
        msg = (f"We couldn't send your weekly payout: {body.get('message', 'please check your bank details')} "
               "Your earnings are safe; withdraw them from Earnings.")
    db.session.add(Notification(user_id=user.id, title=title, message=msg, type="DELIVERY"))
    try:
        if user.email:
            from app.utils.email import send_siiqo_email
            send_siiqo_email(to_email=user.email, subject=f"Siiqo - {title}", template_name="base_notification",
                             first_name=user.first_name or "there", title=title, message=msg,
                             cta_text="Open Earnings", cta_url="https://siiqo.com/partners/dashboard/earnings")
    except Exception as exc:  # noqa: BLE001
        log.info("[PARTNER AUTO PAYOUT] email not sent to partner %s: %s", user.id, exc)


def run_weekly_partner_payouts(now=None) -> dict:
    """Friday job. Off unless PARTNER_AUTO_PAYOUT_ENABLED is on. Sends each
    approved partner's whole available balance (at least MIN_WITHDRAWAL_NGN)
    with send_partner_withdrawal. One run per ISO week."""
    from sqlalchemy.exc import IntegrityError
    from app.models.withdrawal import PartnerPayoutRun
    from app.routes.withdrawal import min_withdrawal_ngn
    from app.services.logistics_flow import partner_earnings
    from app.services.logistics_partners import approved_logistics_partners, partner_access

    if not auto_payout_enabled():
        log.info("[PARTNER AUTO PAYOUT] PARTNER_AUTO_PAYOUT_ENABLED is off; nothing sent")
        return {"enabled": False, "ran": False}

    key = week_key(now)
    if PartnerPayoutRun.query.filter_by(run_key=key).first() is not None:
        return {"enabled": True, "ran": False, "run_key": key, "reason": "already_ran"}
    run = PartnerPayoutRun(run_key=key, status='RUNNING', started_at=_utcnow())
    db.session.add(run)
    try:
        db.session.commit()
    except IntegrityError:
        db.session.rollback()
        return {"enabled": True, "ran": False, "run_key": key, "reason": "already_ran"}

    minimum = Decimal(str(min_withdrawal_ngn()))
    results, paid_total = [], Decimal("0")
    paid = skipped = failed = 0
    for user, app_row in approved_logistics_partners():
        name = (app_row.business_name if app_row and app_row.business_name else user.full_name)
        if not partner_access(user).ok:
            skipped += 1
            results.append({"partner_id": user.id, "name": name, "status": "skipped", "reason": "not_active"})
            continue
        available = partner_earnings(user.id)["available"]
        if available < minimum:
            skipped += 1
            results.append({"partner_id": user.id, "name": name, "status": "skipped",
                            "reason": "below_minimum", "available": str(available)})
            continue
        try:
            code, body = send_partner_withdrawal(user, None, source="auto", run_key=key)
        except Exception as exc:  # noqa: BLE001 - one partner never stops the run
            db.session.rollback()
            log.error("[PARTNER AUTO PAYOUT] partner %s failed: %s", user.id, exc, exc_info=True)
            code, body = 500, {"message": "Unexpected error"}
        row = {"partner_id": user.id, "name": name, "amount": str(available),
               "message": body.get("message"), "reference": body.get("reference"),
               "channel": body.get("channel")}
        if code == 200:
            paid += 1
            paid_total += available
            row["status"] = "sent"
        elif code == 202:
            row["status"] = "processing"   # outcome being checked; amount stays reserved
        elif body.get("code") == "BELOW_MINIMUM":
            skipped += 1
            row["status"] = "skipped"
        else:
            failed += 1
            row["status"] = "failed"
        results.append(row)
        if row["status"] != "skipped":
            try:
                _notify_partner(user, available, code, body)
                db.session.commit()
            except Exception as exc:  # noqa: BLE001
                db.session.rollback()
                log.warning("[PARTNER AUTO PAYOUT] notice not recorded for %s: %s", user.id, exc)

    run = db.session.get(PartnerPayoutRun, run.id)
    run.status = 'DONE'
    run.finished_at = _utcnow()
    run.paid_count, run.skipped_count, run.failed_count = paid, skipped, failed
    run.paid_total = paid_total
    run.results = results
    db.session.commit()
    log.warning("[PARTNER AUTO PAYOUT] %s: %s sent (₦%s), %s skipped, %s failed",
                key, paid, paid_total, skipped, failed)
    return {"enabled": True, "ran": True, "run_key": key, "paid": paid, "paid_total": str(paid_total),
            "skipped": skipped, "failed": failed}


def scheduled_weekly_partner_payouts():
    """Scheduler entry point (named for the cluster-wide advisory lock)."""
    return run_weekly_partner_payouts()
