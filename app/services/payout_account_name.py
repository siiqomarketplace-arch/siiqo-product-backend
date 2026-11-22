"""
Payout account holder names: only ever a name returned by a bank lookup.

The account holder name on a seller's payout account (VendorBankAccount.account_name)
comes from Daya (POST /v1/banks/resolve) or, if Daya can't answer, Paystack
(GET /bank/resolve, app/services/bank_resolve.py). Never the bank's name, the
store name, the user's full name, "Vendor", or anything typed in a browser.

When no name is confirmed the account is saved "name pending":
account_name NULL, is_verified False. Payouts to it are held in the seller's
Siiqo balance until a lookup confirms the name (payout paths call
hold_if_unconfirmed). run_name_retry (hourly) re-checks pending accounts and
tells the seller once when one is fixed. flag_bad_names (boot) marks rows left
by the old fallbacks, with no network calls.

Nothing here logs a full account number.
"""
from __future__ import annotations

import logging
import os
import re
from datetime import datetime, timezone

from app.extensions import db
from app.utils.log_mask import mask_account

log = logging.getLogger(__name__)

HOLD_NOTICE = "We couldn't confirm your bank account name. Check your details in Payouts."
HOLD_TITLE = "Payout on hold: confirm your bank account"
CONFIRMED_TITLE = "Bank account confirmed"

# Names the old code wrote when no lookup answered.
PLACEHOLDER_NAMES = {"", "vendor", "siiqo vendor", "bank", "siiqo seller", "seller"}

# Nigerian banks and wallets (normalised). Used only to recognise a bank name
# saved as a person's name; the live bank list is not needed for that.
KNOWN_BANK_NAMES = {
    "access bank", "access bank plc", "access bank diamond", "citibank nigeria", "citibank",
    "ecobank nigeria", "ecobank", "fidelity bank", "first bank of nigeria", "first bank",
    "firstbank", "first city monument bank", "fcmb", "globus bank", "guaranty trust bank",
    "gtbank", "gtb", "gtco", "heritage bank", "jaiz bank", "keystone bank", "kuda bank",
    "kuda microfinance bank", "kuda", "lotus bank", "moniepoint microfinance bank",
    "moniepoint mfb", "moniepoint", "opay", "opay digital services limited", "paycom",
    "palmpay", "parallex bank", "polaris bank", "premiumtrust bank", "providus bank",
    "stanbic ibtc bank", "stanbic ibtc", "standard chartered bank", "sterling bank",
    "suntrust bank", "taj bank", "titan trust bank", "union bank of nigeria", "union bank",
    "united bank for africa", "uba", "unity bank", "vfd microfinance bank", "vfd", "wema bank",
    "alat by wema", "zenith bank", "carbon", "fairmoney microfinance bank", "fairmoney",
    "paga", "eyowo", "rubies microfinance bank", "sparkle microfinance bank", "optimus bank",
    "signature bank", "coronation merchant bank", "fsdh merchant bank", "rand merchant bank",
    "nova merchant bank", "greenwich merchant bank", "9 payment service bank", "9psb",
    "momo payment service bank", "hope psb", "smartcash payment service bank",
}

_BANK_SUFFIX_RE = re.compile(
    r"(\bbank\b|\bmicrofinance\b|\bmfb\b|\bpsb\b|payment service bank|merchant bank)"
)


def _norm(value) -> str:
    text = re.sub(r"[^a-z0-9 ]+", " ", str(value or "").lower())
    return re.sub(r"\s+", " ", text).strip()


def _utcnow():
    return datetime.now(timezone.utc)


def looks_like_bank_name(name, bank_name=None) -> bool:
    n = _norm(name)
    if not n:
        return False
    if bank_name and n == _norm(bank_name):
        return True
    if n in KNOWN_BANK_NAMES:
        return True
    # "Moniepoint Microfinance Bank", "XYZ MFB", "ABC Bank Plc": a bank, not a person.
    return bool(_BANK_SUFFIX_RE.search(n))


def is_suspect_name(name, bank_name=None, store_names=()) -> bool:
    """True when `name` can't be an account holder name confirmed by a bank:
    empty, a placeholder, a bank's name, or (when given) the store's name."""
    n = _norm(name)
    if n in PLACEHOLDER_NAMES:
        return True
    if looks_like_bank_name(name, bank_name):
        return True
    return any(s and n == _norm(s) for s in store_names)


def name_confirmed(acc) -> bool:
    """The account's name came from a bank lookup and is safe to pay to."""
    return bool(
        acc is not None
        and acc.is_verified
        and (acc.account_name or "").strip()
        and not is_suspect_name(acc.account_name, acc.bank_name)
    )


def name_status(acc) -> str:
    return "verified" if name_confirmed(acc) else "pending"


def mask_number(value) -> str:
    """For display: "******6789"."""
    return mask_account(value)


def shares_word_with(name, full_name) -> bool:
    """Soft fraud signal: does the bank-confirmed name share any word (2+ letters)
    with the seller's own name? Show only, never block."""
    a = {w for w in _norm(name).split() if len(w) >= 2}
    b = {w for w in _norm(full_name).split() if len(w) >= 2}
    if not a or not b:
        return True  # nothing to compare: no warning
    return bool(a & b)


# ---------------------------------------------------------------------------
# Lookup: Daya first, then Paystack
# ---------------------------------------------------------------------------

def _extract_daya_name(data) -> str:
    if not isinstance(data, dict):
        return ""
    inner = data.get("data") if isinstance(data.get("data"), dict) else {}
    return str(data.get("account_name") or inner.get("account_name") or "").strip()


def resolve_holder_name(bank_code, account_number, bank_name=None) -> dict:
    """Ask the bank whose account this is.

    Returns {"status": "resolved" | "rejected" | "unavailable",
             "account_name": str | None, "source": "daya" | "paystack" | None}.
    "rejected" = Paystack answered that the number and bank don't match.
    A name that looks like a bank's name is never accepted.
    """
    bank_code = str(bank_code or "").strip()
    account_number = str(account_number or "").strip()
    if not bank_code or not re.fullmatch(r"\d{10}", account_number):
        return {"status": "rejected", "account_name": None, "source": None}

    from app.services.escrow.paystack_provider import NIBSS_TO_PAYSTACK_BANKS

    if os.environ.get("DAYA_API_KEY"):
        try:
            from app.services import daya_service
            daya_code = daya_service._translate_bank_code(bank_code)
            name = _extract_daya_name(daya_service.resolve_bank_account(daya_code, account_number))
            if name and not is_suspect_name(name, bank_name):
                return {"status": "resolved", "account_name": name[:255], "source": "daya"}
        except Exception as exc:  # noqa: BLE001 (Daya down or account unknown: try Paystack)
            log.info("[ACCOUNT NAME] Daya lookup failed for %s: %s",
                     mask_account(account_number), type(exc).__name__)

    from app.services.bank_resolve import resolve_account
    paystack_code = NIBSS_TO_PAYSTACK_BANKS.get(bank_code, bank_code)
    res = resolve_account(paystack_code, account_number)
    name = (res.get("account_name") or "").strip()
    if res.resolved and name and not is_suspect_name(name, bank_name):
        return {"status": "resolved", "account_name": name[:255], "source": "paystack"}
    if res.rejected:
        return {"status": "rejected", "account_name": None, "source": None}
    return {"status": "unavailable", "account_name": None, "source": None}


# ---------------------------------------------------------------------------
# Updating a saved account
# ---------------------------------------------------------------------------

def _set_recipient(acc, name) -> None:
    """Paystack recipient in the confirmed name (best effort; payouts that need
    one create it again later). Never created without a confirmed name."""
    try:
        from app.services.escrow.paystack_provider import ensure_paystack_transfer_recipient
        from app.models.withdrawal import VendorBankAccount
        rec = ensure_paystack_transfer_recipient(
            account_number=acc.account_number, bank_code=acc.bank_code,
            account_name=name, name_confirmed=True,
        )
        code = rec.get("recipient_code") if rec.get("success") else None
        if code and code != acc.recipient_code:
            clash = VendorBankAccount.query.filter(
                VendorBankAccount.recipient_code == code, VendorBankAccount.id != acc.id).first()
            if clash is None:
                acc.recipient_code = code
    except Exception as exc:  # noqa: BLE001
        log.warning("[ACCOUNT NAME] Recipient not refreshed for account %s: %s", acc.id, exc)


def mark_pending(acc) -> None:
    acc.account_name = None
    acc.is_verified = False
    acc.verified_at = None


def confirm_account(acc, *, refresh_recipient=True) -> dict:
    """One bank lookup for a saved account. Does not commit.

    Returns {"confirmed": bool, "fixed": bool, "status": ...}. "fixed" means the
    account went from unconfirmed to confirmed (or the bank gave a new name).
    A failed lookup never removes a name the bank confirmed earlier; it only
    clears names that can't be real (a bank's name, a placeholder).
    """
    was_confirmed = name_confirmed(acc)
    old_name = acc.account_name
    res = resolve_holder_name(acc.bank_code, acc.account_number, acc.bank_name)
    if res["status"] == "resolved":
        acc.account_name = res["account_name"]
        acc.is_verified = True
        acc.verified_at = _utcnow()
        fixed = (not was_confirmed) or (_norm(old_name) != _norm(res["account_name"]))
        if fixed and refresh_recipient:
            _set_recipient(acc, res["account_name"])
        _sync_storefront_name(acc)
        return {"confirmed": True, "fixed": fixed, "status": "resolved", "source": res["source"]}
    if not was_confirmed:
        mark_pending(acc)
    return {"confirmed": was_confirmed, "fixed": False, "status": res["status"], "source": None}


def _sync_storefront_name(acc) -> None:
    """Keep the storefront's copy of the payout name equal to the bank's."""
    try:
        from app.models.user import Storefront
        sf = Storefront.query.filter_by(vendor_id=acc.vendor_id).first()
        if sf and sf.account_number == acc.account_number and acc.account_name:
            sf.account_name = acc.account_name[:100]
    except Exception:  # noqa: BLE001
        pass


def pending_account_from_storefront(vendor_id):
    """A seller who gave bank details at onboarding but has no payout account row:
    make one, name pending (the storefront's typed name is never used)."""
    from app.models.user import Storefront
    from app.models.withdrawal import VendorBankAccount
    sf = Storefront.query.filter_by(vendor_id=vendor_id).first()
    if not (sf and sf.account_number and sf.bank_code):
        return None
    acc = VendorBankAccount(
        vendor_id=vendor_id, bank_name="Bank", bank_code=sf.bank_code,
        account_number=sf.account_number, account_name=None, is_verified=False,
        is_default=VendorBankAccount.query.filter_by(vendor_id=vendor_id).count() == 0,
    )
    db.session.add(acc)
    db.session.flush()
    return acc


# ---------------------------------------------------------------------------
# Payout hold
# ---------------------------------------------------------------------------

def _notify_hold(vendor_id, order_id=None, amount_ngn=None) -> None:
    from app.models.communication import Notification
    q = Notification.query.filter_by(user_id=vendor_id, title=HOLD_TITLE)
    q = q.filter_by(order_id=order_id) if order_id else q.filter_by(is_read=False)
    if q.first():
        return
    msg = HOLD_NOTICE
    if amount_ngn is not None:
        msg = (f"{HOLD_NOTICE} Your ₦{float(amount_ngn):,.2f}"
               + (f" for Order #{order_id}" if order_id else "")
               + " is safe in your Siiqo balance until then.")
    db.session.add(Notification(user_id=vendor_id, title=HOLD_TITLE, message=msg,
                                type="ESCROW", order_id=order_id))


def hold_if_unconfirmed(acc, *, vendor_id=None, order_id=None, amount_ngn=None) -> bool:
    """True = do NOT send money to this account now.

    Confirmed name: False. Otherwise one bank lookup; if the name is still not
    confirmed the payout stays in the seller's balance and they get the notice
    once. Does not commit (callers hold locks and commit their own work)."""
    if acc is None:
        return False  # "no bank account" has its own handling in each caller
    if name_confirmed(acc):
        return False
    try:
        confirm_account(acc)
    except Exception as exc:  # noqa: BLE001
        log.warning("[ACCOUNT NAME] Lookup before payout failed for account %s: %s", acc.id, exc)
    if name_confirmed(acc):
        return False
    log.warning("[ACCOUNT NAME] Payout held: vendor %s account %s name not confirmed",
                vendor_id or acc.vendor_id, mask_account(acc.account_number))
    try:
        _notify_hold(vendor_id or acc.vendor_id, order_id, amount_ngn)
    except Exception as exc:  # noqa: BLE001
        log.warning("[ACCOUNT NAME] Hold notice not recorded: %s", exc)
    return True


# ---------------------------------------------------------------------------
# Boot backfill and hourly retry
# ---------------------------------------------------------------------------

def flag_bad_names() -> int:
    """Boot-safe, no network: rows whose saved name is a bank's name or a
    placeholder become name pending. The hourly job asks the bank."""
    from app.models.withdrawal import VendorBankAccount
    count = 0
    rows = VendorBankAccount.query.filter(VendorBankAccount.account_name.isnot(None)).all()
    for acc in rows:
        if is_suspect_name(acc.account_name, acc.bank_name):
            mark_pending(acc)
            count += 1
    rows = VendorBankAccount.query.filter(
        VendorBankAccount.account_name.is_(None), VendorBankAccount.is_verified.is_(True)).all()
    for acc in rows:
        acc.is_verified = False
        count += 1
    if count:
        db.session.commit()
        log.info("[ACCOUNT NAME] Marked %s payout account(s) as name pending", count)
    return count


def accounts_needing_check(limit=200):
    """Pending names, plus any row whose name can't be real."""
    from app.models.withdrawal import VendorBankAccount
    from sqlalchemy import or_
    rows = VendorBankAccount.query.filter(or_(
        VendorBankAccount.is_verified.is_(False),
        VendorBankAccount.is_verified.is_(None),
        VendorBankAccount.account_name.is_(None),
    )).order_by(VendorBankAccount.id).limit(limit).all()
    seen = {r.id for r in rows}
    if len(rows) < limit:
        # A verified name equal to the store name is not re-asked every hour:
        # a business account is often named like the store. It is listed for
        # admins (admin_list) instead. Bank names and placeholders are.
        for acc in VendorBankAccount.query.filter(VendorBankAccount.is_verified.is_(True)).all():
            if acc.id not in seen and is_suspect_name(acc.account_name, acc.bank_name):
                rows.append(acc)
                if len(rows) >= limit:
                    break
    return rows


def _notify_confirmed(acc) -> None:
    from app.models.communication import Notification
    db.session.add(Notification(
        user_id=acc.vendor_id, title=CONFIRMED_TITLE, type="ESCROW",
        message=(f"Your bank confirmed the account name: {acc.account_name} "
                 f"({acc.bank_name} {mask_account(acc.account_number)}). "
                 "Payouts can go to this account now. If this isn't you, change it in Payouts."),
    ))


def run_name_retry(limit=200) -> dict:
    """Hourly: re-check pending payout account names and fix the ones the bank
    now confirms. The seller is told once, when the account is fixed."""
    fixed = checked = 0
    for acc in accounts_needing_check(limit):
        checked += 1
        try:
            result = confirm_account(acc)
            if result["fixed"]:
                fixed += 1
                _notify_confirmed(acc)
            db.session.commit()
        except Exception as exc:  # noqa: BLE001
            db.session.rollback()
            log.warning("[ACCOUNT NAME] Retry failed for account %s: %s", acc.id, exc)
    if checked:
        log.info("[ACCOUNT NAME] Retry checked %s account(s), fixed %s", checked, fixed)
    return {"checked": checked, "fixed": fixed}


# ---------------------------------------------------------------------------
# Admin view
# ---------------------------------------------------------------------------

def admin_row(acc) -> dict:
    from app.models.user import Storefront, User
    user = db.session.get(User, acc.vendor_id)
    sf = Storefront.query.filter_by(vendor_id=acc.vendor_id).first()
    store = sf.store_name if sf else None
    confirmed = name_confirmed(acc)
    reasons = []
    if not confirmed:
        reasons.append("name_pending")
    if acc.account_name and looks_like_bank_name(acc.account_name, acc.bank_name):
        reasons.append("bank_name")
    if acc.account_name and store and _norm(acc.account_name) == _norm(store):
        reasons.append("store_name")
    mismatch = bool(confirmed and user and not shares_word_with(acc.account_name, user.full_name))
    return {
        "id": acc.id,
        "vendor": {"id": acc.vendor_id, "full_name": user.full_name if user else None,
                   "store_name": store},
        "bank_name": acc.bank_name,
        "account_number_masked": mask_account(acc.account_number),
        "account_name": acc.account_name if confirmed else None,
        "status": "verified" if confirmed else "pending",
        "reasons": reasons,
        "name_mismatch_warning": mismatch,
        "is_default": bool(acc.is_default),
        "verified_at": acc.verified_at.isoformat() if acc.verified_at else None,
    }


def admin_list() -> list:
    """Accounts to check: name pending, name equal to a bank or store name, or a
    confirmed name sharing no word with the seller's own name (warning only)."""
    from app.models.withdrawal import VendorBankAccount
    out = []
    for acc in VendorBankAccount.query.order_by(VendorBankAccount.id.desc()).limit(2000).all():
        row = admin_row(acc)
        if row["reasons"] or row["name_mismatch_warning"]:
            out.append(row)
    return out
