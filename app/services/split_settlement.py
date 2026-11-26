"""Was the seller already paid by a gateway split at checkout? Only with proof.

Incident (Order 639, 26 Nov 2026): a Flutterwave checkout asked for a
subaccount split and stored a "SPLIT:" marker; Flutterwave settled the whole
payment to Siiqo, the release trusted the marker, sent nothing, wrote a payout
DEBIT and the seller was never paid. So:

  * Asking for a split is never proof. A seller counts as "paid by split" ONLY
    when the gateway's own record of that payment shows the split:
    ``escrow_transactions.split_settled`` is True AND ``split_proof`` says what
    proved it. Both are written at payment confirmation (record_at_confirmation),
    never at initiation.
  * Paystack: the verified transaction (GET /transaction/verify/:reference,
    https://paystack.com/docs/payments/verify-payments/ ; mirror
    https://docs-v2.paystack.com/docs/payments/verify-payments/) carries
    ``subaccount`` and ``fees_split`` for a single split ("fees_split":
    {"paystack":..,"integration":..,"subaccount":<kobo>}) and ``split`` for a
    multi-split. Proof = the seller's requested subaccount code AND at least
    the seller's share in kobo.
  * Flutterwave: the verify-transaction response documents no per-payment
    split field (https://developer.flutterwave.com/api/transactions/verify-transaction);
    split amounts only appear later in settlements
    (https://developer.flutterwave.com/api/settlements/get-a-settlement).
    There is no proof at confirmation, so Siiqo no longer asks Flutterwave
    for splits at all (routes/payments.py, routes/payment_links.py): all
    Flutterwave money settles to Siiqo and Siiqo transfers the seller's share
    at release. A "SPLIT:" marker on an older row is NOT proof.
  * Unproven -> Siiqo sends the transfer at release (or the money stays in the
    seller's withdrawable balance if it can't be sent). Never both.
  * Paystack, split requested but not yet decided (older rows, or the
    confirmation could not read the gateway): checked with Paystack once at
    release. If Paystack can't be reached the money stays in the seller's
    balance (no transfer, no debit) and an ERROR is logged.

Every release path asks ``vendor_paid_by_split`` and nothing else.

Past releases: ``split_mismatches`` lists released orders whose payout was
recorded (a PAYOUT- DEBIT) with neither proof of a split nor a tracked
transfer nor a recorded manual payout, for both gateways and single- or
multi-seller payments. Admin actions per order: restore to the seller's
balance, or record the manual payout a person already sent.
"""
import logging
import re
from datetime import date, datetime
from decimal import Decimal, InvalidOperation

from sqlalchemy import and_, cast, exists, literal, or_, String

from app.extensions import db

log = logging.getLogger(__name__)

SPLIT_MARKER = "SPLIT:"
PROOF_MAX = 255
PAYOUT_DESC = "Automated gateway payout settlement"


def _refs(escrow) -> set:
    """Identifiers of the gateway payment this escrow belonged to.
    payment_ref is the reliable one (set on every escrow of a payment);
    the others cover rows made before it existed."""
    payment_ref = getattr(escrow, "payment_ref", None)
    if payment_ref:
        return {str(payment_ref)}
    refs = set()
    for value in (escrow.payscrow_ref, escrow.transaction_number, escrow.payscrow_transaction_id):
        if value:
            v = str(value)
            if v.startswith(SPLIT_MARKER):
                v = v[len(SPLIT_MARKER):]
            refs.add(v)
    return refs


def transaction_vendor_ids(escrow, order=None) -> set:
    """Sellers whose orders were paid in the same gateway payment as this escrow."""
    from app.models.escrow import EscrowTransaction
    from app.models.order import Order

    vendor_ids = set()
    if order is not None and order.vendor_id:
        vendor_ids.add(order.vendor_id)
    refs = _refs(escrow)
    if not refs:
        return vendor_ids
    rows = (
        db.session.query(Order.vendor_id)
        .join(EscrowTransaction, EscrowTransaction.order_id == Order.id)
        .filter(or_(
            EscrowTransaction.payment_ref.in_(refs),
            EscrowTransaction.transaction_number.in_(refs),
            EscrowTransaction.payscrow_ref.in_(refs),
            EscrowTransaction.payscrow_transaction_id.in_(refs),
        ))
        .all()
    )
    vendor_ids.update(r[0] for r in rows if r[0])
    return vendor_ids


def vendor_paystack_code(vendor_id):
    try:
        from app.services.escrow.paystack_provider import vendor_paystack_subaccount
        return vendor_paystack_subaccount(vendor_id)
    except Exception as exc:  # noqa: BLE001
        log.warning("[SPLIT] Subaccount lookup failed for vendor %s: %s", vendor_id, exc)
        return None


def vendor_has_paystack_subaccount(vendor_id) -> bool:
    return bool(vendor_paystack_code(vendor_id))


# ── what the OLD code believed (used only to find past damage / old rows) ──

def legacy_split_marker(order, escrow, gateway: str) -> bool:
    """True when the code before this fix would have treated the seller as
    paid by split and sent nothing: a Flutterwave "SPLIT:" marker, a Paystack
    flag set at initiation, or an older Paystack row whose seller has a
    subaccount. Never proof."""
    if gateway == "flutterwave":
        return bool(escrow.payscrow_ref and str(escrow.payscrow_ref).startswith(SPLIT_MARKER))
    if gateway == "paystack":
        if escrow.split_settled is True:
            return True
        if escrow.split_settled is None:
            return vendor_has_paystack_subaccount(order.vendor_id)
    return False


# ── proof ────────────────────────────────────────────────────────────────────

def is_proven(escrow) -> bool:
    return escrow is not None and escrow.split_settled is True and bool(escrow.split_proof)


def _seller_share_kobo(order, escrow) -> int:
    """Everything this seller is owed from the payment this escrow belongs to
    (all of their orders in it), in kobo."""
    from app.models.order import Order
    from app.services.payment_refs import escrows_for_payment, gateway_ref, seller_net_by_vendor
    ref = gateway_ref(escrow)
    orders = []
    if ref:
        ids = [e.order_id for e in escrows_for_payment(ref)]
        if ids:
            orders = [o for o in Order.query.filter(Order.id.in_(ids)).all() if o.vendor_id == order.vendor_id]
    if not orders:
        orders = [order]
    net = seller_net_by_vendor(orders).get(order.vendor_id, Decimal("0"))
    return int((net * 100).to_integral_value())


def _kobo(value):
    try:
        return int(Decimal(str(value)))
    except (InvalidOperation, TypeError, ValueError):
        return None


def paystack_split_amount(txn: dict, code: str):
    """Kobo Paystack says it sent to subaccount ``code`` in this verified
    transaction, or None when the transaction shows no split to it."""
    if not isinstance(txn, dict) or not code:
        return None
    sub = txn.get("subaccount") or {}
    fees_split = txn.get("fees_split") or {}
    if isinstance(sub, dict) and sub.get("subaccount_code") == code and isinstance(fees_split, dict):
        amount = _kobo(fees_split.get("subaccount"))
        if amount is not None:
            return amount
    split = txn.get("split") or {}
    if isinstance(split, dict):
        shares = split.get("shares") or {}
        for item in (shares.get("subaccounts") or []) if isinstance(shares, dict) else []:
            if isinstance(item, dict) and item.get("subaccount_code") == code:
                amount = _kobo(item.get("amount"))
                if amount is not None:
                    return amount
    return None


def looks_like_paystack_transaction(txn) -> bool:
    """A full transaction object (verify response / charge.success data), so
    the absence of a split in it is a real answer."""
    return isinstance(txn, dict) and txn.get("status") == "success" and (
        "subaccount" in txn or "fees_split" in txn or "split" in txn)


def paystack_split_requested(order, escrow) -> bool:
    """A split may have been asked for on this payment."""
    if escrow.split_requested:
        return True
    if escrow.split_settled is True and not escrow.split_proof:
        return True   # older row: flag set at initiation
    if escrow.split_settled is None and escrow.split_requested is None:
        # Older row with nothing recorded: the old rule (single seller with a
        # subaccount) might have attached one.
        return len(transaction_vendor_ids(escrow, order)) == 1 and vendor_has_paystack_subaccount(order.vendor_id)
    return False


def _decide_paystack(order, escrow, txn) -> bool | None:
    """Record the decision from a verified Paystack transaction. Returns True
    (proven split), False (no split) or None (txn says nothing)."""
    if not looks_like_paystack_transaction(txn):
        return None
    code = escrow.split_requested if (escrow.split_requested and escrow.split_requested.startswith("ACCT_")) \
        else vendor_paystack_code(order.vendor_id)
    amount = paystack_split_amount(txn, code)
    expected = _seller_share_kobo(order, escrow)
    if amount is not None and amount >= expected - 100:   # within ₦1
        escrow.split_settled = True
        escrow.split_proof = (f"paystack verify: {amount} kobo to {code} "
                              f"(seller share {expected} kobo)")[:PROOF_MAX]
        log.info("[SPLIT] Order #%s: Paystack split proven (%s kobo to %s)", order.id, amount, code)
        return True
    escrow.split_settled = False
    if amount is not None:
        escrow.split_proof = None
        log.error("[SPLIT] Order #%s: Paystack split to %s was %s kobo, less than the seller's %s kobo; "
                  "not counted as paid by split", order.id, code, amount, expected)
    else:
        log.info("[SPLIT] Order #%s: Paystack verification shows no split to the seller", order.id)
    return False


def record_at_confirmation(order, escrow, gateway: str, txn: dict | None = None) -> None:
    """Called when a payment is confirmed, before any delivery/release.
    Flutterwave/Daya: no split (Siiqo pays the seller). Paystack: decided from
    the verified transaction when a split was requested; left undecided when
    the transaction data says nothing (checked again at release)."""
    if order is None or escrow is None or is_proven(escrow):
        return
    if gateway != "paystack":
        escrow.split_settled = False
        return
    if not paystack_split_requested(order, escrow):
        escrow.split_settled = False
        return
    _decide_paystack(order, escrow, txn)


def _verify_paystack(escrow):
    from app.services.escrow.paystack_provider import PaystackProvider
    from app.services.payment_refs import gateway_ref
    ref = gateway_ref(escrow)
    if not ref:
        return None
    try:
        res = PaystackProvider().verify_transaction(ref)
    except Exception as exc:  # noqa: BLE001
        log.error("[SPLIT] Paystack verify failed for %s: %s", ref, exc)
        return None
    return res.get("raw") if isinstance(res, dict) else None


def split_decision(order, escrow, gateway: str, allow_verify: bool = True) -> str:
    """'split' (proven), 'no_split', or 'unknown' (Paystack could not be asked)."""
    if is_proven(escrow):
        return "split"
    if gateway != "paystack":
        return "no_split"        # Flutterwave "SPLIT:" markers are never proof
    if escrow.split_settled is False:
        return "no_split"
    if not paystack_split_requested(order, escrow):
        return "no_split"
    if not allow_verify:
        return "unknown"
    decided = _decide_paystack(order, escrow, _verify_paystack(escrow))
    if decided is None:
        log.error("[SPLIT] Order #%s: a Paystack split was requested and could not be checked; "
                  "payout kept in the seller's balance", order.id)
        return "unknown"
    return "split" if decided else "no_split"


def vendor_paid_by_split(order, escrow, gateway: str, allow_verify: bool = True) -> bool:
    """True only when the gateway's own record PROVES it paid this seller."""
    return split_decision(order, escrow, gateway, allow_verify=allow_verify) == "split"


# ── past releases: payouts recorded without proof ───────────────────────────

_ORDER_RE = re.compile(r"Order #(\d+)")
MAX_CANDIDATES = 1000


def restore_reference(order_id: int) -> str:
    return f"RESTORE-PAYOUT-{order_id}"


def _payout_debit_refs(escrow, order):
    refs = [f"PAYOUT-{order.id}"]
    if escrow.transaction_number:
        refs.insert(0, f"PAYOUT-{escrow.transaction_number}")
    return refs


def _candidate_query(include_untracked: bool):
    """Released escrows with a PAYOUT- DEBIT and no proof, no tracked transfer
    and no manual payout. One indexed query; no per-row lookups."""
    from app.models.escrow import EscrowStatus, EscrowTransaction
    from app.models.finance import Ledger
    from app.models.order import Order
    from app.models.payouts import ManualPayout, PayoutTransfer
    from app.models.user import Storefront
    from app.models.withdrawal import VendorBankAccount

    debit = exists().where(and_(
        Ledger.vendor_id == Order.vendor_id,
        Ledger.transaction_type == "DEBIT",
        Ledger.reference_id.in_([literal("PAYOUT-") + EscrowTransaction.transaction_number,
                                 literal("PAYOUT-") + cast(Order.id, String)]),
    ))
    tracked = exists().where(and_(PayoutTransfer.kind == "order", PayoutTransfer.order_id == Order.id))
    manual = exists().where(ManualPayout.order_id == Order.id)
    q = (db.session.query(EscrowTransaction, Order)
         .join(Order, Order.id == EscrowTransaction.order_id)
         .filter(EscrowTransaction.status == EscrowStatus.RELEASED, debit, ~tracked, ~manual)
         .filter(or_(EscrowTransaction.split_settled.isnot(True), EscrowTransaction.split_proof.is_(None))))
    if not include_untracked:
        has_sub = or_(
            exists().where(and_(VendorBankAccount.vendor_id == Order.vendor_id,
                                VendorBankAccount.paystack_subaccount_code.isnot(None))),
            exists().where(and_(Storefront.vendor_id == Order.vendor_id,
                                Storefront.paystack_subaccount_code.isnot(None))),
        )
        q = q.filter(or_(
            EscrowTransaction.payscrow_ref.like(f"{SPLIT_MARKER}%"),
            EscrowTransaction.split_settled.is_(True),
            and_(EscrowTransaction.split_settled.is_(None), has_sub),
        ))
    return q.order_by(EscrowTransaction.released_at.desc().nullslast(), EscrowTransaction.id.desc())


def _subaccount_vendors(vendor_ids) -> set:
    from app.models.user import Storefront
    from app.models.withdrawal import VendorBankAccount
    if not vendor_ids:
        return set()
    ids = set(r[0] for r in db.session.query(VendorBankAccount.vendor_id).filter(
        VendorBankAccount.vendor_id.in_(vendor_ids), VendorBankAccount.paystack_subaccount_code.isnot(None)))
    ids |= set(r[0] for r in db.session.query(Storefront.vendor_id).filter(
        Storefront.vendor_id.in_(vendor_ids), Storefront.paystack_subaccount_code.isnot(None)))
    return ids


def _gateway(order, escrow) -> str:
    from app.services.disputes import gateway_for
    return gateway_for(order, escrow)


def split_mismatches(page: int = 1, per_page: int = 50, include_untracked: bool = False) -> dict:
    """Released orders whose payout was recorded as paid with no proof.

    kind 'split_unproven': the old code trusted a split marker and sent
    nothing (the Order 639 case; any gateway, single or multi-seller).
    kind 'transfer_untracked' (only with include_untracked): the old code sent
    a transfer the gateway accepted, but its final status was never recorded.
    Bounded: at most MAX_CANDIDATES most recent releases are examined, with a
    fixed number of queries per page."""
    from app.models.finance import Ledger
    from app.models.user import Storefront

    page = max(1, int(page or 1))
    per_page = max(1, min(int(per_page or 50), 200))
    pairs = _candidate_query(include_untracked).limit(MAX_CANDIDATES).all()
    vendor_ids = {o.vendor_id for _, o in pairs}
    subs = _subaccount_vendors(vendor_ids)

    rows = []
    for escrow, order in pairs:
        try:
            gateway = _gateway(order, escrow)
            if gateway == "flutterwave":
                trusted = bool(escrow.payscrow_ref and str(escrow.payscrow_ref).startswith(SPLIT_MARKER))
            elif gateway == "paystack":
                trusted = escrow.split_settled is True or (escrow.split_settled is None and order.vendor_id in subs)
            else:
                trusted = False
            if not trusted and not include_untracked:
                continue
            rows.append((escrow, order, gateway, "split_unproven" if trusted else "transfer_untracked"))
        except Exception as exc:  # noqa: BLE001 - one bad row never breaks the list
            log.warning("[PAYOUT CHECK] Order #%s skipped: %s", getattr(order, "id", "?"), exc)

    total = len(rows)
    page_rows = rows[(page - 1) * per_page: page * per_page]
    ids = [o.id for _, o, _, _ in page_rows]
    vids = {o.vendor_id for _, o, _, _ in page_rows}
    debit_refs = [r for e, o, _, _ in page_rows for r in _payout_debit_refs(e, o)]
    debits = {}
    if debit_refs:
        for d in Ledger.query.filter(Ledger.transaction_type == "DEBIT", Ledger.reference_id.in_(debit_refs)):
            debits.setdefault((d.vendor_id, d.reference_id), d)
    restored = set()
    if ids:
        restored = {r[0] for r in db.session.query(Ledger.reference_id).filter(
            Ledger.transaction_type == "CREDIT",
            Ledger.reference_id.in_([restore_reference(i) for i in ids]))}
    stores = {}
    if vids:
        stores = {s.vendor_id: s.store_name for s in Storefront.query.filter(Storefront.vendor_id.in_(vids))}

    out = []
    for escrow, order, gateway, kind in page_rows:
        d = next((debits[(order.vendor_id, r)] for r in _payout_debit_refs(escrow, order)
                  if (order.vendor_id, r) in debits), None)
        if d is None:
            continue
        out.append({
            "order_id": order.id,
            "vendor": {"id": order.vendor_id, "store_name": stores.get(order.vendor_id)},
            "gateway": gateway,
            "kind": kind,
            "amount": str(Decimal(str(d.amount)).quantize(Decimal("0.01"))),
            "paid_at": d.created_at.isoformat() if getattr(d, "created_at", None) else None,
            "released_at": escrow.released_at.isoformat() if escrow.released_at else None,
            "transaction_ref": escrow.payment_ref or escrow.payscrow_ref or escrow.transaction_number,
            "debit_reference": d.reference_id,
            "restored": restore_reference(order.id) in restored,
        })
    return {"orders": out, "total": total, "page": page, "per_page": per_page,
            "pages": max(1, -(-total // per_page)), "capped": len(pairs) >= MAX_CANDIDATES}


def find_mismatch(order_id: int):
    """The listed row (either kind) for one order, or None."""
    from app.models.finance import Ledger
    from app.models.order import Order
    from app.models.user import Storefront
    pair = _candidate_query(include_untracked=True).filter(Order.id == int(order_id)).first()
    if pair is None:
        return None
    escrow, order = pair
    d = (Ledger.query.filter(Ledger.vendor_id == order.vendor_id, Ledger.transaction_type == "DEBIT",
                             Ledger.reference_id.in_(_payout_debit_refs(escrow, order))).first())
    if d is None:
        return None
    gateway = _gateway(order, escrow)
    sf = Storefront.query.filter_by(vendor_id=order.vendor_id).first()
    return {
        "order_id": order.id,
        "vendor": {"id": order.vendor_id, "store_name": sf.store_name if sf else None},
        "gateway": gateway,
        "kind": "split_unproven" if legacy_split_marker(order, escrow, gateway) else "transfer_untracked",
        "amount": str(Decimal(str(d.amount)).quantize(Decimal("0.01"))),
        "paid_at": d.created_at.isoformat() if d.created_at else None,
        "released_at": escrow.released_at.isoformat() if escrow.released_at else None,
        "transaction_ref": escrow.payment_ref or escrow.payscrow_ref or escrow.transaction_number,
        "debit_reference": d.reference_id,
        "restored": Ledger.query.filter_by(vendor_id=order.vendor_id, transaction_type="CREDIT",
                                           reference_id=restore_reference(order.id)).first() is not None,
    }


def restore_mismatch(order_id: int):
    """Write the compensating CREDIT for one listed order. Returns
    (row, created) or (None, False) when the order is not listed.
    Idempotent: a second call finds the RESTORE- credit and writes nothing.
    Refused (None, False) once a manual payout was recorded for the order."""
    from app.models.communication import Notification
    from app.routes.escrow import _credit_vendor_ledger

    row = find_mismatch(order_id)
    if row is None:
        return None, False
    if row["restored"]:
        return row, False
    amount = float(row["amount"])
    vendor_id = row["vendor"]["id"]
    _credit_vendor_ledger(
        vendor_id=vendor_id,
        amount=amount,
        reference_id=restore_reference(order_id),
        description=f"Restored payout for Order #{order_id} (it was never sent)",
    )
    db.session.add(Notification(
        user_id=vendor_id,
        title="Money added to your balance",
        message=(f"₦{amount:,.2f} from Order #{order_id} is now in your Siiqo balance. "
                 "You can withdraw it."),
        type="ESCROW",
        order_id=order_id,
    ))
    return dict(row, restored=True), True


def mismatch_total(rows) -> str:
    total = sum((Decimal(r["amount"]) for r in rows if not r.get("restored")), Decimal("0"))
    return str(total.quantize(Decimal("0.01")))


# ── manual payouts ───────────────────────────────────────────────────────────

MANUAL_GATEWAYS = ("flutterwave", "paystack", "daya", "bank")


class ManualPayoutError(ValueError):
    pass


def _parse_date(value):
    if isinstance(value, date):
        return value
    try:
        return datetime.strptime(str(value).strip()[:10], "%Y-%m-%d").date()
    except (TypeError, ValueError):
        raise ManualPayoutError("Date must be YYYY-MM-DD.")


def record_manual_payout(order_id: int, *, amount, gateway: str, reference: str, paid_on,
                         note: str = "", admin_id=None):
    """Mark this order's payout as settled by a transfer a person already sent
    by hand. Does NOT change the seller's balance. Returns (record, created).
    Idempotent: the same order recorded again returns the first record; a
    different reference for an order already recorded is refused. Refused when
    the order was restored to the seller's balance (they would be paid twice)."""
    from app.models.escrow import EscrowStatus, EscrowTransaction
    from app.models.finance import Ledger
    from app.models.order import Order
    from app.models.payouts import ManualPayout

    order = db.session.get(Order, int(order_id))
    if order is None:
        raise ManualPayoutError(f"Order #{order_id} not found.")
    escrow = EscrowTransaction.query.filter_by(order_id=order.id).first()
    if escrow is None or escrow.status != EscrowStatus.RELEASED:
        raise ManualPayoutError(f"Order #{order.id} has not been released, so there is no payout to record.")
    reference = (reference or "").strip()[:120]
    gateway = (gateway or "").strip().lower()
    if not reference:
        raise ManualPayoutError("Transfer reference is required.")
    if gateway not in MANUAL_GATEWAYS:
        raise ManualPayoutError(f"Gateway must be one of: {', '.join(MANUAL_GATEWAYS)}.")
    try:
        amt = Decimal(str(amount)).quantize(Decimal("0.01"))
    except (InvalidOperation, TypeError, ValueError):
        raise ManualPayoutError("Amount must be a number.")
    if amt <= 0:
        raise ManualPayoutError("Amount must be more than zero.")
    day = _parse_date(paid_on)

    existing = ManualPayout.query.filter_by(order_id=order.id).first()
    if existing is not None:
        if existing.reference != reference:
            raise ManualPayoutError(
                f"Order #{order.id} already has a manual payout recorded (ref {existing.reference}).")
        return existing, False
    if Ledger.query.filter_by(vendor_id=order.vendor_id, transaction_type="CREDIT",
                              reference_id=restore_reference(order.id)).first():
        raise ManualPayoutError(
            f"Order #{order.id} was already restored to the seller's balance. Recording a manual payout "
            "too would count it as paid twice.")
    rec = ManualPayout(order_id=order.id, vendor_id=order.vendor_id, amount=amt, gateway=gateway,
                       reference=reference, paid_on=day, note=(note or "").strip()[:500] or None,
                       recorded_by_admin_id=admin_id)
    db.session.add(rec)
    db.session.flush()
    return rec, True


def check_with_gateway(order_id: int) -> dict:
    """Paystack only: ask Paystack whether this listed order's payment was
    really split, and record the proof if so (then it leaves the list)."""
    from app.models.escrow import EscrowTransaction
    from app.models.order import Order
    order = db.session.get(Order, int(order_id))
    escrow = EscrowTransaction.query.filter_by(order_id=int(order_id)).first() if order else None
    if order is None or escrow is None:
        return {"checked": False, "message": "Order not found."}
    gateway = _gateway(order, escrow)
    if gateway != "paystack":
        return {"checked": False, "message": "Only Paystack shows a split on the payment. Check the "
                                             "Flutterwave dashboard, then restore or record the manual payout."}
    txn = _verify_paystack(escrow)
    decided = _decide_paystack(order, escrow, txn)
    if decided is None:
        return {"checked": False, "message": "Paystack couldn't be reached. Try again later."}
    return {"checked": True, "split_proven": bool(decided),
            "message": ("Paystack shows the split paid this seller. Nothing is owed." if decided
                        else "Paystack shows no split to this seller. Restore it or record the manual payout.")}
