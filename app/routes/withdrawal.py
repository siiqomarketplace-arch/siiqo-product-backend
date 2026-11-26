import logging
from app.utils.log_mask import mask_account
"""
withdrawal.py — Vendor Withdrawal Routes
Handles bank accounts, withdrawal requests, and POD payments
"""
import os
import requests
from datetime import datetime, timezone
from decimal import Decimal

from flask import Blueprint, g, request, jsonify
from flask_jwt_extended import jwt_required, get_jwt_identity
from sqlalchemy import func

from app.extensions import db
from app.models.withdrawal import VendorBankAccount, Withdrawal, PODPayment, PartnerBankAccount, PartnerWithdrawal
from app.models.order import Order
from app.models.finance import Ledger
from app.models.communication import Notification
from app.models.user import User
from app.services.logistics_partners import partner_required

withdrawal_bp = Blueprint('withdrawal', __name__)


def min_withdrawal_ngn() -> Decimal:
    """Smallest seller withdrawal, in naira (env MIN_WITHDRAWAL_NGN, default 500).
    Low enough that one sale of a ₦500 or ₦1,000 digital product can be
    withdrawn. A bad or non-positive value falls back to the default."""
    raw = (os.environ.get("MIN_WITHDRAWAL_NGN") or "").strip()
    try:
        value = Decimal(raw) if raw else Decimal("500")
    except Exception:  # noqa: BLE001
        value = Decimal("500")
    return value if value > 0 else Decimal("500")

PAYSTACK_SECRET_KEY = os.environ.get('PAYSTACK_SECRET_KEY', '')
PAYSTACK_BASE_URL = 'https://api.paystack.co'


def _utcnow():
    return datetime.now(timezone.utc)


def _get_user_id():
    """Helper to get user_id as integer from JWT"""
    return int(get_jwt_identity())


def _get_ledger_balance(vendor_id: int) -> Decimal:
    """Calculate vendor's available ledger balance"""
    credits = db.session.query(func.sum(Ledger.amount)).filter_by(
        vendor_id=vendor_id, transaction_type='CREDIT'
    ).scalar() or Decimal('0')
    
    debits = db.session.query(func.sum(Ledger.amount)).filter_by(
        vendor_id=vendor_id, transaction_type='DEBIT'
    ).scalar() or Decimal('0')
    
    return Decimal(str(credits)) - Decimal(str(debits))


def _debit_ledger(vendor_id: int, amount: Decimal, description: str, reference_id: str):
    """Write a DEBIT entry to vendor's ledger"""
    balance = _get_ledger_balance(vendor_id)
    balance_after = balance - amount
    
    db.session.add(Ledger(
        vendor_id=vendor_id,
        transaction_type='DEBIT',
        amount=amount,
        description=description,
        reference_id=reference_id,
        balance_after=balance_after,
    ))


ACCOUNT_NOT_CONFIRMED = "We couldn't confirm this account. Check the number and bank, or try again."
NAME_PENDING_MESSAGE = ("Bank account saved. We couldn't confirm the account name yet; "
                        "we'll keep trying. Payouts wait until it's confirmed.")
_REJECTED = object()


def _confirmed_name_and_recipient(bank_code, account_number, bank_name):
    """(account_name | None | _REJECTED, recipient_code | None, verified).

    The name is only ever one a bank lookup returned. A Paystack recipient is
    created only with that name; with no confirmed name there is none yet."""
    from app.services.payout_account_name import resolve_holder_name
    res = resolve_holder_name(bank_code, account_number, bank_name)
    if res["status"] == "rejected":
        return _REJECTED, None, False
    if res["status"] != "resolved":
        logging.info("[BANK ACCOUNT] Name not confirmed for %s; saving as name pending",
                     mask_account(account_number))
        return None, None, False
    name = res["account_name"]
    recipient_code = None
    try:
        from app.services.escrow.paystack_provider import ensure_paystack_transfer_recipient
        rec = ensure_paystack_transfer_recipient(account_number=account_number, bank_code=bank_code,
                                                 account_name=name, name_confirmed=True)
        if rec.get("success"):
            recipient_code = rec.get("recipient_code")
    except Exception as exc:  # noqa: BLE001 (payout paths create it later)
        logging.warning("[BANK ACCOUNT] Recipient not created: %s", type(exc).__name__)
    return name, recipient_code, True


def _apply_name(acc, account_name, verified):
    """New details on a saved row: a confirmed name, or name pending."""
    if verified and account_name:
        acc.account_name = account_name
        acc.is_verified = True
        acc.verified_at = _utcnow()
    else:
        acc.account_name = None
        acc.is_verified = False
        acc.verified_at = None


# ---------------------------------------------------------------------------
# BANK ACCOUNT MANAGEMENT
# ---------------------------------------------------------------------------


@withdrawal_bp.route('/bank-accounts', methods=['GET'])
@jwt_required()
def get_bank_accounts():
    """Get vendor's bank accounts"""
    vendor_id = _get_user_id()
    accounts = VendorBankAccount.query.filter_by(vendor_id=vendor_id).all()
    if not accounts:
        try:
            # Bank details given at onboarding: saved name pending. The name
            # typed there is never used; the hourly check asks the bank.
            from app.services.payout_account_name import pending_account_from_storefront
            new_acc = pending_account_from_storefront(vendor_id)
            if new_acc is not None:
                db.session.commit()
                accounts = [new_acc]
        except Exception as _e:
            db.session.rollback()
            logging.warning(f"[BANK ACCOUNTS] Auto-sync from storefront error: {_e}")

    return jsonify({
        'status': 'success',
        'accounts': [acc.to_dict() for acc in accounts]
    }), 200


@withdrawal_bp.route('/bank-accounts', methods=['POST'])
@jwt_required()
def add_bank_account():
    """Add and verify a new bank account"""
    vendor_id = _get_user_id()
    data = request.get_json() or {}
    
    bank_code = data.get('bank_code')
    account_number = data.get('account_number')
    
    if not bank_code or not account_number:
        return jsonify({'message': 'bank_code and account_number required'}), 400
    
    # Normalize bank codes for Paystack and CBN/Daya
    from app.services.escrow.paystack_provider import NIBSS_TO_PAYSTACK_BANKS, PAYSTACK_TO_NIBSS_BANKS
    paystack_bank_code = NIBSS_TO_PAYSTACK_BANKS.get(str(bank_code).strip(), str(bank_code).strip())
    cbn_bank_code = PAYSTACK_TO_NIBSS_BANKS.get(str(bank_code).strip(), str(bank_code).strip())

    # The account holder name only ever comes from a bank lookup (Daya, then
    # Paystack). Not confirmed -> saved as name pending, no recipient yet.
    try:
        bank_name = (data.get('bank_name') or '').strip() or 'Bank'
        account_name, recipient_code, verified = _confirmed_name_and_recipient(
            bank_code, account_number, bank_name)
        if account_name is _REJECTED:
            return jsonify({'message': ACCOUNT_NOT_CONFIRMED}), 400

        # 3. Check if account already exists
        existing = VendorBankAccount.query.filter_by(
            vendor_id=vendor_id,
            account_number=account_number,
        ).first()
        
        if existing:
            # Update recipient_code and details on existing record
            if recipient_code:
                existing.recipient_code = recipient_code
            existing.bank_name = bank_name or existing.bank_name
            existing.bank_code = cbn_bank_code or existing.bank_code
            _apply_name(existing, account_name, verified)
            db.session.commit()
            # Verification 2-3: a payout account in a different name opens a re-check.
            from app.services.verification.rechecks import check_after_change
            check_after_change(vendor_id)
            return jsonify({
                'status': 'success',
                'message': 'Bank account updated successfully',
                'account': existing.to_dict()
            }), 200
        
        # 4. Create bank account record (save CBN code for Daya + recipient_code for Paystack)
        is_first = VendorBankAccount.query.filter_by(vendor_id=vendor_id).count() == 0
        
        bank_account = VendorBankAccount(
            vendor_id=vendor_id,
            bank_name=bank_name,
            bank_code=cbn_bank_code,
            account_number=account_number,
            account_name=account_name if verified else None,
            recipient_code=recipient_code,
            is_verified=verified,
            verified_at=_utcnow() if verified else None,
            is_default=is_first  # First account is default
        )
        
        db.session.add(bank_account)
        db.session.flush()  # Get bank_account.id without committing yet

        # ── Create Paystack subaccount for Split Payments ──────────────────
        # Register the vendor's bank account as a Paystack subaccount so
        # digital/service checkouts can split the payment natively.
        try:
            from app.services.escrow.paystack_provider import create_paystack_subaccount
            from app.models.user import Storefront
            storefront = Storefront.query.filter_by(vendor_id=vendor_id).first()
            business_name = (storefront.store_name if storefront else (account_name or "Siiqo seller"))
            sub_result = create_paystack_subaccount(
                business_name=business_name,
                bank_code=bank_code,
                account_number=account_number,
            )
            if sub_result.get("success"):
                subaccount_code = sub_result["subaccount_code"]
                bank_account.paystack_subaccount_code = subaccount_code
                # Also update the storefront so checkout can find it via either lookup
                if storefront:
                    storefront.paystack_subaccount_code = subaccount_code
                logging.info(
                    f"[BANK ACCOUNT] Paystack subaccount {subaccount_code} "
                    f"saved for vendor {vendor_id}"
                )
            else:
                logging.warning(
                    f"[BANK ACCOUNT] Paystack subaccount creation failed for vendor {vendor_id}: "
                    f"{sub_result.get('error_message')}"
                )
        except Exception as _sub_exc:
            logging.warning(
                f"[BANK ACCOUNT] Paystack subaccount error for vendor {vendor_id}: {_sub_exc}"
            )

        # ── Create Flutterwave subaccount for Split Payments ───────────────
        # Best-effort — if it fails, Paystack and Daya payouts still work.
        # Flutterwave subaccount is used when buyers pay via Flutterwave
        # (international cards, local cards). The RS_xxx ID is stored on the
        # bank account record and looked up at checkout time.
        try:
            from app.services.flutterwave_service import create_subaccount as _flw_create_sub, is_configured as _flw_configured
            if _flw_configured():
                # Storefront is already loaded above; safe to reuse
                _flw_business_name = (storefront.store_name if storefront else (account_name or "Siiqo seller"))
                _flw_phone = ""
                if storefront:
                    try:
                        from app.models.user import User as _U
                        _vendor_user = db.session.get(_U, int(vendor_id))
                        _flw_phone = (_vendor_user.phone or "") if _vendor_user else ""
                    except Exception:
                        pass

                flw_result = _flw_create_sub(
                    business_name=_flw_business_name,
                    # Flutterwave uses the same CBN bank code as Daya
                    bank_code=cbn_bank_code,
                    account_number=account_number,
                    business_mobile=_flw_phone or "08012345678",
                    business_email=(storefront.vendor.email if storefront and storefront.vendor else ""),
                )
                if flw_result.get("success"):
                    bank_account.flw_subaccount_id = flw_result["subaccount_id"]
                    logging.info(
                        "[BANK ACCOUNT] Flutterwave subaccount %s saved for vendor %s",
                        flw_result["subaccount_id"], vendor_id,
                    )
                else:
                    logging.warning(
                        "[BANK ACCOUNT] Flutterwave subaccount creation failed for vendor %s: %s",
                        vendor_id, flw_result.get("error_message"),
                    )
            else:
                logging.info("[BANK ACCOUNT] Flutterwave not configured — skipping FLW subaccount creation")
        except Exception as _flw_exc:
            logging.warning(
                "[BANK ACCOUNT] Flutterwave subaccount error for vendor %s: %s",
                vendor_id, _flw_exc,
            )

        db.session.commit()
        # Verification 2-3: a payout account in a different name opens a re-check.
        from app.services.verification.rechecks import check_after_change
        check_after_change(vendor_id)
        
        return jsonify({
            'status': 'success',
            'message': ('Bank account added and verified successfully' if verified
                        else NAME_PENDING_MESSAGE),
            'account': bank_account.to_dict()
        }), 201

    except Exception as e:
        db.session.rollback()
        logging.warning(f'[BANK ACCOUNT] Save failed for vendor {vendor_id}: {type(e).__name__}')
        return jsonify({'message': 'Could not save the bank account. Please try again.'}), 500



@withdrawal_bp.route('/bank-accounts/<int:account_id>/set-default', methods=['PATCH'])
@jwt_required()
def set_default_account(account_id):
    """Set a bank account as default"""
    vendor_id = _get_user_id()
    
    account = db.session.get(VendorBankAccount, account_id)
    if not account or account.vendor_id != int(vendor_id):
        return jsonify({'message': 'Account not found'}), 404
    
    # Remove default from all accounts
    VendorBankAccount.query.filter_by(vendor_id=vendor_id).update({'is_default': False})
    
    # Set this as default
    account.is_default = True
    db.session.commit()
    # Verification 2-3: a payout account in a different name opens a re-check.
    from app.services.verification.rechecks import check_after_change
    check_after_change(vendor_id)
    
    return jsonify({
        'status': 'success',
        'message': 'Default account updated'
    }), 200


@withdrawal_bp.route('/bank-accounts/<int:account_id>', methods=['DELETE'])
@jwt_required()
def delete_bank_account(account_id):
    """Delete a bank account"""
    vendor_id = _get_user_id()
    
    account = db.session.get(VendorBankAccount, account_id)
    if not account or account.vendor_id != int(vendor_id):
        return jsonify({'message': 'Account not found'}), 404
    
    # Can't delete if it's the only account and there are pending withdrawals
    if account.is_default:
        other_accounts = VendorBankAccount.query.filter(
            VendorBankAccount.vendor_id == vendor_id,
            VendorBankAccount.id != account_id
        ).first()
        
        if other_accounts:
            other_accounts.is_default = True
    
    db.session.delete(account)
    db.session.commit()
    # Verification 2-3: a payout account in a different name opens a re-check.
    from app.services.verification.rechecks import check_after_change
    check_after_change(vendor_id)
    
    return jsonify({
        'status': 'success',
        'message': 'Bank account deleted'
    }), 200


@withdrawal_bp.route('/bank-accounts/<int:account_id>/recheck', methods=['POST'])
@jwt_required()
def recheck_bank_account(account_id):
    """"Check again" on the Payouts page: ask the bank for the account holder
    name now (Daya, then Paystack). Confirmed -> verified; else still pending."""
    from app.services.payout_account_name import confirm_account
    vendor_id = _get_user_id()
    account = db.session.get(VendorBankAccount, account_id)
    if not account or account.vendor_id != int(vendor_id):
        return jsonify({'message': 'Account not found'}), 404
    result = confirm_account(account)
    db.session.commit()
    if result['confirmed']:
        return jsonify({'status': 'success', 'confirmed': True,
                        'message': f'Confirmed: {account.account_name}',
                        'account': account.to_dict()}), 200
    return jsonify({'status': 'pending', 'confirmed': False,
                    'message': ("We still couldn't confirm the account name. "
                                "Check the number and bank, or try again later. We'll keep trying."),
                    'account': account.to_dict()}), 200


# ---------------------------------------------------------------------------
# WITHDRAWAL REQUESTS
# ---------------------------------------------------------------------------

@withdrawal_bp.route('/balance', methods=['GET'])
@jwt_required()
def get_balance():
    """Get vendor's available balance"""
    vendor_id = _get_user_id()
    balance = _get_ledger_balance(vendor_id)
    
    # Get pending withdrawals
    pending_amount = db.session.query(func.sum(Withdrawal.amount)).filter_by(
        vendor_id=vendor_id,
        status='PENDING'
    ).scalar() or Decimal('0')
    
    # Withdrawals are debited when requested, so the ledger balance already
    # excludes them; subtracting pending again would count them twice.
    available = balance
    
    return jsonify({
        'status': 'success',
        'balance': str(balance),
        'pending_withdrawals': str(pending_amount),
        'available': str(available),
        'currency': 'NGN'
    }), 200


@withdrawal_bp.route('/withdrawals', methods=['GET'])
@jwt_required()
def get_withdrawals():
    """Get vendor's withdrawal history"""
    vendor_id = _get_user_id()
    page = int(request.args.get('page', 1))
    per_page = min(int(request.args.get('per_page', 20)), 100)
    
    paginated = Withdrawal.query.filter_by(vendor_id=vendor_id).order_by(
        Withdrawal.requested_at.desc()
    ).paginate(page=page, per_page=per_page, error_out=False)
    
    return jsonify({
        'status': 'success',
        'withdrawals': [w.to_dict() for w in paginated.items],
        'total': paginated.total,
        'pages': paginated.pages,
        'page': page
    }), 200


@withdrawal_bp.route('/withdrawals', methods=['POST'])
@jwt_required()
def request_withdrawal():
    """
    Vendor requests a withdrawal.

    With Paystack provider: triggers a Paystack transfer to the vendor's
    default bank account using the stored recipient_code.

    With Payscrow provider: returns 400 (split settlement is automatic).
    """
    from app.services import flutterwave_service as _flw
    provider = os.environ.get("ACTIVE_ESCROW_PROVIDER", "paystack").lower()
    has_flw = _flw.is_configured()
    has_paystack = (provider == "paystack") or bool(PAYSTACK_SECRET_KEY)

    if not has_flw and not has_paystack:
        return jsonify({
            'message': (
                'Manual withdrawals are currently deactivated. '
                'Escrow payouts are processed automatically upon delivery confirmation.'
            )
        }), 400

    vendor_id = _get_user_id()
    data = request.get_json() or {}

    # Money only goes to an account whose holder name the bank confirmed. One
    # lookup now (before any lock); still unconfirmed -> it stays in the balance.
    from app.services.payout_account_name import hold_if_unconfirmed, name_confirmed, HOLD_NOTICE
    pre_acc = (VendorBankAccount.query.filter_by(vendor_id=vendor_id, is_default=True).first()
               or VendorBankAccount.query.filter_by(vendor_id=vendor_id).first())
    if pre_acc is not None and not name_confirmed(pre_acc):
        held = hold_if_unconfirmed(pre_acc, vendor_id=int(vendor_id))
        db.session.commit()
        if held:
            return jsonify({'message': HOLD_NOTICE, 'code': 'ACCOUNT_NAME_NOT_CONFIRMED'}), 400

    # Serialise withdrawals per vendor: hold a row lock on the vendor until the
    # withdrawal and its DEBIT are committed, so two requests can't both spend
    # the same balance.
    from app.models.user import User
    db.session.query(User).filter_by(id=vendor_id).with_for_update().first()

    # Withdrawals are debited when requested, so the ledger balance is what
    # is still owed to the vendor.
    available = _get_ledger_balance(int(vendor_id))

    MIN_WITHDRAWAL = min_withdrawal_ngn()
    if available < MIN_WITHDRAWAL:
        db.session.rollback()
        return jsonify({
            'message': f'Insufficient balance. Minimum withdrawal is ₦{MIN_WITHDRAWAL:,.2f}. '
                       f'Your available balance is ₦{available:,.2f}.'
        }), 400

    # Get vendor's default bank account
    bank_acc = VendorBankAccount.query.filter_by(
        vendor_id=vendor_id, is_default=True
    ).first()
    if not bank_acc:
        bank_acc = VendorBankAccount.query.filter_by(vendor_id=vendor_id).first()

    if not bank_acc or not bank_acc.account_number or not bank_acc.bank_code:
        db.session.rollback()
        return jsonify({
            'message': 'No verified bank account found. '
                       'Please add and verify a bank account in your payout settings first.'
        }), 400

    if not name_confirmed(bank_acc):
        # Re-checked under the lock, no network: never send with an unconfirmed name.
        db.session.rollback()
        return jsonify({'message': HOLD_NOTICE, 'code': 'ACCOUNT_NAME_NOT_CONFIRMED'}), 400

    if not has_flw and not bank_acc.recipient_code:
        db.session.rollback()
        return jsonify({'message': 'Paystack recipient not set up for this bank account.'}), 400

    requested_amount = data.get('amount')
    if requested_amount:
        try:
            requested_amount = Decimal(str(requested_amount))
        except Exception:
            db.session.rollback()
            return jsonify({'message': 'Invalid amount format.'}), 400
        if requested_amount <= 0 or requested_amount > available:
            db.session.rollback()
            return jsonify({'message': 'Invalid withdrawal amount.'}), 400
        if requested_amount < MIN_WITHDRAWAL:
            db.session.rollback()
            return jsonify({'message': f'The smallest withdrawal is ₦{MIN_WITHDRAWAL:,.2f}.'}), 400
    else:
        requested_amount = available  # default: withdraw all

    import uuid as _uuid
    reference = f"WD-{_uuid.uuid4().hex[:12].upper()}"

    # Record the withdrawal and its DEBIT before any money moves.
    withdrawal = Withdrawal(
        vendor_id=int(vendor_id),
        amount=requested_amount,
        fee_amount=Decimal('0'),
        net_amount=requested_amount,
        status='PENDING',
        bank_account_id=bank_acc.id,
        transfer_reference=reference,
    )
    db.session.add(withdrawal)
    _debit_ledger(
        vendor_id=int(vendor_id),
        amount=requested_amount,
        description=f"Withdrawal to {bank_acc.bank_name} {bank_acc.account_number[-4:]}",
        reference_id=reference,
    )
    db.session.commit()

    # Primary transfer channel: Flutterwave if configured, else Paystack
    transfer_code, error_message = "", None
    sent_by, sent_status = None, None
    try:
        result = None
        if has_flw:
            flw_res = _flw.transfer_to_vendor(
                account_bank=bank_acc.bank_code,
                account_number=bank_acc.account_number,
                amount_ngn=float(requested_amount),
                reference=reference,
                narration="Siiqo vendor withdrawal",
            )
            if flw_res.get("success"):
                transfer_code = flw_res.get("transfer_id", "")
                sent_by, sent_status = "flutterwave", flw_res.get("status")
            elif has_paystack and bank_acc.recipient_code:
                # Flutterwave rejected it; fall back to Paystack
                from app.services.escrow.paystack_provider import paystack_transfer_to_vendor
                result = paystack_transfer_to_vendor(
                    recipient_code=bank_acc.recipient_code,
                    amount_ngn=float(requested_amount),
                    reference=reference,
                    reason="Siiqo vendor withdrawal",
                )
                if not result.get("success"):
                    error_message = result.get('error_message', flw_res.get('error_message'))
            else:
                error_message = flw_res.get('error_message', 'Payment gateway error')
        else:
            from app.services.escrow.paystack_provider import paystack_transfer_to_vendor
            result = paystack_transfer_to_vendor(
                recipient_code=bank_acc.recipient_code,
                amount_ngn=float(requested_amount),
                reference=reference,
                reason="Siiqo vendor withdrawal",
            )
            if not result.get("success"):
                error_message = result.get('error_message', 'Unknown error.')
        if result and result.get("success"):
            transfer_code = result.get("transfer_code", "")
            sent_by, sent_status = "paystack", result.get("status")
    except Exception as exc:
        # We can't tell whether the money left. Keep the DEBIT and leave the
        # withdrawal PENDING for an admin to check against the gateway.
        logging.error(f"[WITHDRAWAL] {reference} transfer outcome unknown: {exc}", exc_info=True)
        withdrawal.failure_reason = f"Transfer outcome unknown: {exc}"[:500]
        db.session.commit()
        return jsonify({
            'message': 'Your withdrawal is being checked. If it does not arrive within 24 hours, contact support@siiqo.com.',
            'amount': str(requested_amount),
        }), 202

    if error_message:
        # The gateway refused it: no money left, so give the balance back.
        withdrawal.status = 'FAILED'
        withdrawal.failure_reason = str(error_message)[:500]
        withdrawal.processed_at = _utcnow()
        balance_after = _get_ledger_balance(int(vendor_id)) + requested_amount
        db.session.add(Ledger(
            vendor_id=int(vendor_id),
            transaction_type='CREDIT',
            amount=requested_amount,
            description=f"Reversal of failed withdrawal {reference}",
            reference_id=f"{reference}-REVERSAL",
            balance_after=balance_after,
        ))
        db.session.commit()
        return jsonify({'message': f"Withdrawal failed: {error_message}"}), 400

    withdrawal.status = 'SENT'
    withdrawal.transfer_code = transfer_code
    withdrawal.processed_at = _utcnow()
    # Followed to its final status; a failed/reversed transfer gives the
    # money back to the balance once (app/services/payout_tracking.py).
    try:
        from app.services.payout_tracking import record_transfer
        record_transfer(kind="withdrawal", user_id=int(vendor_id), gateway=sent_by or "paystack",
                        reference=reference, amount=requested_amount, gateway_transfer_id=transfer_code,
                        withdrawal_id=withdrawal.id, debit_reference=reference, gateway_status=sent_status)
    except Exception as _track_err:  # noqa: BLE001
        logging.error(f"[WITHDRAWAL] Could not record transfer {reference}: {_track_err}")
    db.session.commit()

    return jsonify({
        'status': 'success',
        'message': 'Withdrawal initiated. Funds will arrive within 24 hours.',
        'transfer_code': transfer_code,
        'amount': str(requested_amount),
    }), 200


# ---------------------------------------------------------------------------
# PAY ON DELIVERY (POD) MANAGEMENT
# ---------------------------------------------------------------------------

@withdrawal_bp.route('/pod-payments', methods=['GET'])
@jwt_required()
def get_pod_payments():
    """Get vendor's POD payments"""
    vendor_id = _get_user_id()
    page = int(request.args.get('page', 1))
    per_page = min(int(request.args.get('per_page', 20)), 100)
    status = request.args.get('status')  # confirmed, pending, reconciled
    
    query = PODPayment.query.filter_by(vendor_id=vendor_id)
    
    if status == 'confirmed':
        query = query.filter_by(confirmed_by_vendor=True, reconciled=False)
    elif status == 'pending':
        query = query.filter_by(confirmed_by_vendor=False)
    elif status == 'reconciled':
        query = query.filter_by(reconciled=True)
    
    paginated = query.order_by(PODPayment.created_at.desc()).paginate(
        page=page, per_page=per_page, error_out=False
    )
    
    return jsonify({
        'status': 'success',
        'payments': [p.to_dict() for p in paginated.items],
        'total': paginated.total,
        'pages': paginated.pages,
        'page': page
    }), 200


@withdrawal_bp.route('/pod-payments/<int:order_id>/confirm', methods=['POST'])
@jwt_required()
def confirm_pod_payment(order_id):
    """Vendor confirms cash payment received"""
    vendor_id = _get_user_id()
    data = request.get_json() or {}
    
    # Get order
    order = db.session.get(Order, order_id)
    if not order:
        return jsonify({'message': 'Order not found'}), 404
    
    if order.vendor_id != int(vendor_id):
        return jsonify({'message': 'Unauthorized'}), 403

    if order.payment_method != 'POD':
        return jsonify({'message': 'This endpoint is only for Pay-on-Delivery (POD) orders.'}), 400

    if order.status == 'COMPLETED':
        return jsonify({'message': 'This order has already been completed.'}), 400
    
    # Get or create POD payment record
    pod_payment = PODPayment.query.filter_by(order_id=order_id).first()
    
    if not pod_payment:
        pod_payment = PODPayment(
            order_id=order_id,
            vendor_id=vendor_id,
            amount=order.total_amount,
            currency='NGN'
        )
        db.session.add(pod_payment)
    
    # Confirm payment
    pod_payment.confirmed_by_vendor = True
    pod_payment.confirmed_at = _utcnow()
    pod_payment.payment_method = data.get('payment_method', 'CASH')
    pod_payment.vendor_notes = data.get('notes', '')
    
    # Update order status
    order.status = 'COMPLETED'
    
    try:
        from app.services.referral_service import check_and_reward_referral_on_order_complete
        check_and_reward_referral_on_order_complete(order)
    except Exception as ex:
        logging.error(f"[REFERRAL ERR] POD vendor confirm referral reward failed: {ex}")
    
    # Notify buyer
    if order.buyer_id:
        db.session.add(Notification(
            user_id=order.buyer_id,
            title='Order Completed',
            message=f'Order #{order.id} has been completed. Thank you for shopping!',
            type='ORDER',
            order_id=order.id
        ))
    
    db.session.commit()
    
    return jsonify({
        'status': 'success',
        'message': 'Payment confirmed successfully',
        'payment': pod_payment.to_dict()
    }), 200


@withdrawal_bp.route('/pod-payments/summary', methods=['GET'])
@jwt_required()
def get_pod_summary():
    """Get POD payment summary for vendor"""
    vendor_id = _get_user_id()
    
    total_pending = db.session.query(func.sum(PODPayment.amount)).filter_by(
        vendor_id=vendor_id,
        confirmed_by_vendor=False
    ).scalar() or Decimal('0')
    
    total_confirmed = db.session.query(func.sum(PODPayment.amount)).filter_by(
        vendor_id=vendor_id,
        confirmed_by_vendor=True,
        reconciled=False
    ).scalar() or Decimal('0')
    
    total_reconciled = db.session.query(func.sum(PODPayment.amount)).filter_by(
        vendor_id=vendor_id,
        reconciled=True
    ).scalar() or Decimal('0')
    
    pending_count = PODPayment.query.filter_by(
        vendor_id=vendor_id,
        confirmed_by_vendor=False
    ).count()
    
    return jsonify({
        'status': 'success',
        'summary': {
            'total_pending': str(total_pending),
            'total_confirmed': str(total_confirmed),
            'total_reconciled': str(total_reconciled),
            'pending_count': pending_count,
            'currency': 'NGN'
        }
    }), 200


# ---------------------------------------------------------------------------
# PAYSTACK BANKS LIST
# ---------------------------------------------------------------------------

@withdrawal_bp.route('/banks', methods=['GET'])
def get_banks():
    """Get list of Nigerian banks from Paystack — public, no auth required"""
    try:
        headers = {
            'Authorization': f'Bearer {PAYSTACK_SECRET_KEY}',
        }
        
        response = requests.get(
            f'{PAYSTACK_BASE_URL}/bank',
            headers=headers,
            params={'country': 'nigeria', 'perPage': 200},
            timeout=10
        )
        
        data = response.json()
        
        if data.get('status') and data.get('data'):
            banks = [
                {'name': b['name'], 'code': b['code']}
                for b in data['data']
            ]
            # Sort alphabetically
            banks.sort(key=lambda x: x['name'])
            return jsonify({
                'status': 'success',
                'banks': banks
            }), 200
        else:
            logging.warning(f'[BANKS] Paystack returned no data: {data}')
            return jsonify({'status': 'success', 'banks': []}), 200
            
    except Exception as e:
        logging.warning(f'[BANKS] Error fetching bank list: {e}')
        return jsonify({'status': 'success', 'banks': []}), 200


@withdrawal_bp.route('/banks/resolve', methods=['GET', 'POST'])
def resolve_bank_account():
    """
    Resolve (verify) a bank account name — public endpoint.
    Used by the partner apply form and vendor onboarding to auto-fill account name.

    POST (preferred): JSON body {account_number, bank_code} — keeps the account
    number out of access logs.
    GET (deprecated, kept for old app builds): query params bank_code, account_number.
    """
    if request.method == 'POST':
        body = request.get_json(silent=True) or {}
        bank_code      = str(body.get('bank_code') or '').strip()
        account_number = str(body.get('account_number') or '').strip()
    else:
        logging.info('[BANKS] Deprecated GET /withdrawal/banks/resolve used '
                     '(account number in URL); callers should POST a JSON body.')
        bank_code      = (request.args.get('bank_code') or '').strip()
        account_number = (request.args.get('account_number') or '').strip()

    if not bank_code or not account_number:
        return jsonify({'message': 'bank_code and account_number are required'}), 400

    if len(account_number) != 10:
        return jsonify({'message': 'Account number must be exactly 10 digits'}), 400

    # Daya first, then Paystack (app/services/payout_account_name.py). The name
    # shown is the bank's; a bank's own name is never returned as a holder name.
    try:
        from app.services.payout_account_name import resolve_holder_name
        res = resolve_holder_name(bank_code, account_number)
    except Exception as e:  # noqa: BLE001
        logging.warning(f'[BANKS] Account resolve error: {type(e).__name__}')
        res = {'status': 'unavailable', 'account_name': None}

    if res['status'] == 'resolved':
        return jsonify({
            'status': 'success',
            'account_name': res['account_name'],
            'account_number': account_number,
        }), 200
    if res['status'] == 'rejected':
        return jsonify({'message': ACCOUNT_NOT_CONFIRMED, 'account_name': None}), 400
    return jsonify({'message': "We couldn't reach the bank to confirm this account. Please try again.",
                    'account_name': None}), 503


# ---------------------------------------------------------------------------
# LOGISTICS PARTNER BANK ACCOUNT MANAGEMENT & WITHDRAWALS
# ---------------------------------------------------------------------------

@withdrawal_bp.route('/partner/bank-accounts', methods=['GET'])
@jwt_required()
@partner_required
def get_partner_bank_accounts():
    """Get partner's bank accounts"""
    partner_id = _get_user_id()
    accounts = PartnerBankAccount.query.filter_by(partner_id=partner_id).all()
    return jsonify({
        'status': 'success',
        'accounts': [acc.to_dict() for acc in accounts]
    }), 200


@withdrawal_bp.route('/partner/bank-accounts', methods=['POST'])
@jwt_required()
@partner_required
def add_partner_bank_account():
    """Add and verify a new partner bank account"""
    partner_id = _get_user_id()
    data = request.get_json() or {}
    
    bank_code = data.get('bank_code')
    account_number = data.get('account_number')
    
    if not bank_code or not account_number:
        return jsonify({'message': 'bank_code and account_number required'}), 400
    
    try:
        headers = {
            'Authorization': f'Bearer {PAYSTACK_SECRET_KEY}',
            'Content-Type': 'application/json'
        }
        
        # 1. Resolve account name
        resolve_url = f'{PAYSTACK_BASE_URL}/bank/resolve'
        resolve_params = {
            'account_number': account_number,
            'bank_code': bank_code
        }
        resolve_response = requests.get(resolve_url, headers=headers, params=resolve_params, timeout=10)
        resolve_data = resolve_response.json()
        
        if not resolve_data.get('status'):
            return jsonify({'message': 'Could not verify account. Please check details.'}), 400
        
        account_name = resolve_data['data']['account_name']
        bank_name = data.get('bank_name', 'Bank')
        
        # 2. Create transfer recipient
        recipient_url = f'{PAYSTACK_BASE_URL}/transferrecipient'
        recipient_payload = {
            'type': 'nuban',
            'name': account_name,
            'account_number': account_number,
            'bank_code': bank_code,
            'currency': 'NGN'
        }
        recipient_response = requests.post(recipient_url, headers=headers, json=recipient_payload, timeout=10)
        recipient_data = recipient_response.json()
        
        if not recipient_data.get('status'):
            return jsonify({'message': 'Could not create recipient. Please try again.'}), 400
        
        recipient_code = recipient_data['data']['recipient_code']
        
        # 3. Check if account already exists
        existing = PartnerBankAccount.query.filter_by(
            partner_id=partner_id,
            account_number=account_number,
            bank_code=bank_code
        ).first()
        
        if existing:
            return jsonify({'message': 'This bank account is already added'}), 400
        
        # 4. Create bank account record
        is_first = PartnerBankAccount.query.filter_by(partner_id=partner_id).count() == 0
        
        bank_account = PartnerBankAccount(
            partner_id=partner_id,
            bank_name=bank_name,
            bank_code=bank_code,
            account_number=account_number,
            account_name=account_name,
            recipient_code=recipient_code,
            is_verified=True,
            verified_at=_utcnow(),
            is_default=is_first  # First account is default
        )
        
        db.session.add(bank_account)
        db.session.commit()
        
        return jsonify({
            'status': 'success',
            'message': 'Bank account added and verified successfully',
            'account': bank_account.to_dict()
        }), 201
        
    except requests.exceptions.RequestException as e:
        return jsonify({'message': f'Verification failed: {str(e)}'}), 500
    except Exception as e:
        db.session.rollback()
        return jsonify({'message': f'Error: {str(e)}'}), 500


@withdrawal_bp.route('/partner/bank-accounts/<int:account_id>/set-default', methods=['PATCH'])
@jwt_required()
@partner_required
def set_default_partner_account(account_id):
    """Set a partner bank account as default"""
    partner_id = _get_user_id()
    
    account = db.session.get(PartnerBankAccount, account_id)
    if not account or account.partner_id != int(partner_id):
        return jsonify({'message': 'Account not found'}), 404
    
    # Remove default from all accounts
    PartnerBankAccount.query.filter_by(partner_id=partner_id).update({'is_default': False})
    
    # Set this as default
    account.is_default = True
    db.session.commit()
    
    return jsonify({
        'status': 'success',
        'message': 'Default account updated'
    }), 200


@withdrawal_bp.route('/partner/bank-accounts/<int:account_id>', methods=['DELETE'])
@jwt_required()
@partner_required
def delete_partner_bank_account(account_id):
    """Delete a partner bank account"""
    partner_id = _get_user_id()
    
    account = db.session.get(PartnerBankAccount, account_id)
    if not account or account.partner_id != int(partner_id):
        return jsonify({'message': 'Account not found'}), 404
    
    if account.is_default:
        other_accounts = PartnerBankAccount.query.filter(
            PartnerBankAccount.partner_id == partner_id,
            PartnerBankAccount.id != account_id
        ).first()
        
        if other_accounts:
            other_accounts.is_default = True
    
    db.session.delete(account)
    db.session.commit()
    
    return jsonify({
        'status': 'success',
        'message': 'Bank account deleted'
    }), 200


@withdrawal_bp.route('/partner/balance', methods=['GET'])
@jwt_required()
@partner_required
def get_partner_balance():
    """A logistics partner's money: a delivery fee counts once the order's
    payment is final (Safe Pay released, or Pay on Delivery cash confirmed)."""
    from app.services.logistics_flow import partner_earnings
    money = partner_earnings(g.partner.id)
    return jsonify({
        'status': 'success',
        'balance': str(money['earned']),
        'pending_earnings': str(money['pending']),
        'pending_withdrawals': str(money['withdrawn']),
        'available': str(money['available']),
        'currency': 'NGN'
    }), 200


@withdrawal_bp.route('/partner/withdrawals', methods=['GET'])
@jwt_required()
@partner_required
def get_partner_withdrawals():
    """Get partner's withdrawal history"""
    partner_id = g.partner.id
    try:
        page = max(1, int(request.args.get('page', 1)))
        per_page = min(max(1, int(request.args.get('per_page', 20))), 100)
    except (TypeError, ValueError):
        page, per_page = 1, 20

    paginated = PartnerWithdrawal.query.filter_by(partner_id=partner_id).order_by(
        PartnerWithdrawal.requested_at.desc()
    ).paginate(page=page, per_page=per_page, error_out=False)

    return jsonify({
        'status': 'success',
        'withdrawals': [w.to_dict() for w in paginated.items],
        'total': paginated.total,
        'pages': paginated.pages,
        'page': page
    }), 200


def partner_withdrawal_reference(withdrawal) -> str:
    """One fixed transfer reference per withdrawal row: a retried transfer
    reuses it, so the gateway refuses to send the money twice."""
    import hashlib
    digest = hashlib.sha256(f"partner-withdrawal:{withdrawal.id}:{withdrawal.partner_id}".encode()
                            ).hexdigest()[:10].upper()
    return f"PWD-{withdrawal.partner_id}-{withdrawal.id}-{digest}"


@withdrawal_bp.route('/partner/withdrawals', methods=['POST'])
@jwt_required()
@partner_required
def request_partner_withdrawal():
    """Approved partner withdraws earned delivery fees to its saved bank:
    Daya NGN transfer first, Paystack only if Daya isn't configured or
    definitely refused it (app/services/partner_payouts.py)."""
    from app.services.partner_payouts import send_partner_withdrawal
    data = request.get_json(silent=True) or {}
    status, body = send_partner_withdrawal(g.partner, data.get('amount'), source='manual')
    return jsonify(body), status


# ---------------------------------------------------------------------------
# DAYA-NATIVE BANK ACCOUNT REGISTRATION
# Separate from Paystack path — fetches banks and verifies accounts via Daya.
# Bank codes stored here are CBN codes that work directly with Daya transfers.
# ---------------------------------------------------------------------------

@withdrawal_bp.route('/daya-banks', methods=['GET'])
def get_daya_banks():
    """
    Return Nigerian banks from Daya's bank list.
    These use CBN bank codes (not Paystack internal codes) so they work
    directly with Daya /v1/transfers without any translation.
    Public endpoint — no auth required.
    """
    from app.services import daya_service
    try:
        data = daya_service._request(
            "GET",
            "/v1/banks",
            headers=daya_service._headers(),
        )
        banks = data.get("data", [])
        formatted = sorted(
            [{"name": b["name"], "code": b["code"]} for b in banks if b.get("code") and b.get("name")],
            key=lambda x: x["name"],
        )
        return jsonify({"status": "success", "banks": formatted}), 200
    except Exception as exc:
        logging.warning("[DAYA BANKS] Failed to fetch: %s — falling back to empty list", exc)
        return jsonify({"status": "success", "banks": []}), 200


@withdrawal_bp.route('/daya-bank-account', methods=['POST'])
@jwt_required()
def add_daya_bank_account():
    """
    Register a vendor payout bank account using Daya's verification.
    Bank codes are Daya/CBN-native — no Paystack involved.

    Used for Daya-path payouts (NGN onramp + crypto orders).
    Completely separate from Paystack bank account registration.

    Body: { bank_code, account_number, bank_name }
    """
    from app.services import daya_service

    vendor_id = _get_user_id()
    data = request.get_json() or {}

    bank_code = (data.get("bank_code") or "").strip()
    account_number = (data.get("account_number") or "").strip()
    bank_name = (data.get("bank_name") or "").strip()

    if not bank_code or not account_number:
        return jsonify({"message": "bank_code and account_number are required"}), 400

    if len(account_number) != 10 or not account_number.isdigit():
        return jsonify({"message": "Account number must be exactly 10 digits"}), 400

    # Steps 1-2: the account holder name only ever comes from a bank lookup
    # (Daya, then Paystack). A Paystack recipient is created only in that
    # name. Neither answers -> saved name pending (NULL, unverified); payouts
    # wait and the hourly check keeps asking the bank.
    account_name, recipient_code, verified = _confirmed_name_and_recipient(
        bank_code, account_number, bank_name)
    if account_name is _REJECTED:
        return jsonify({"message": ACCOUNT_NOT_CONFIRMED}), 400

    # Step 3: Check for duplicate (same account_number OR same recipient_code for this vendor)
    dup_filter = VendorBankAccount.account_number == account_number
    if recipient_code:
        dup_filter = db.or_(dup_filter, VendorBankAccount.recipient_code == recipient_code)
    existing = VendorBankAccount.query.filter(
        VendorBankAccount.vendor_id == vendor_id, dup_filter).first()

    if existing:
        # Update existing record with latest details
        if recipient_code:
            existing.recipient_code = recipient_code
        if bank_code:
            existing.bank_code = bank_code
        if bank_name:
            existing.bank_name = bank_name
        _apply_name(existing, account_name, verified)
        
        # Update Paystack subaccount if missing
        if not existing.paystack_subaccount_code:
            try:
                from app.services.escrow.paystack_provider import create_paystack_subaccount
                from app.models.user import Storefront
                storefront = Storefront.query.filter_by(vendor_id=vendor_id).first()
                business_name = storefront.store_name if storefront else (account_name or "Siiqo seller")
                sub_result = create_paystack_subaccount(
                    business_name=business_name,
                    bank_code=bank_code,
                    account_number=account_number,
                )
                if sub_result.get("success"):
                    existing.paystack_subaccount_code = sub_result["subaccount_code"]
                    if storefront:
                        storefront.paystack_subaccount_code = sub_result["subaccount_code"]
                    logging.info("[DAYA BANK REG] Paystack subaccount %s created for vendor %s",
                                 sub_result["subaccount_code"], vendor_id)
            except Exception as exc:
                logging.warning("[DAYA BANK REG] Paystack subaccount error for vendor %s: %s", vendor_id, exc)
        
        # Update Flutterwave subaccount if missing
        if not existing.flw_subaccount_id:
            try:
                from app.services.flutterwave_service import create_subaccount as _flw_create_sub, is_configured as _flw_configured
                if _flw_configured():
                    from app.models.user import Storefront, User as _U
                    storefront = Storefront.query.filter_by(vendor_id=vendor_id).first()
                    _flw_business_name = storefront.store_name if storefront else (account_name or "Siiqo seller")
                    _flw_phone = ""
                    if storefront:
                        try:
                            _vendor_user = db.session.get(_U, int(vendor_id))
                            _flw_phone = (_vendor_user.phone or "") if _vendor_user else ""
                        except Exception:
                            pass
                    
                    flw_result = _flw_create_sub(
                        business_name=_flw_business_name,
                        bank_code=bank_code,
                        account_number=account_number,
                        business_mobile=_flw_phone or "08012345678",
                        business_email=(storefront.vendor.email if storefront and storefront.vendor else ""),
                    )
                    if flw_result.get("success"):
                        existing.flw_subaccount_id = flw_result["subaccount_id"]
                        logging.info("[DAYA BANK REG] Flutterwave subaccount %s created for vendor %s",
                                     flw_result["subaccount_id"], vendor_id)
                else:
                    logging.info("[DAYA BANK REG] Flutterwave not configured — skipping FLW subaccount creation")
            except Exception as _flw_exc:
                logging.warning("[DAYA BANK REG] Flutterwave subaccount error for vendor %s: %s", vendor_id, _flw_exc)
        
        db.session.commit()
        # Verification 2-3: a payout account in a different name opens a re-check.
        from app.services.verification.rechecks import check_after_change
        check_after_change(vendor_id)
        return jsonify({"status": "success",
                        "message": ("Bank account updated successfully" if verified else NAME_PENDING_MESSAGE),
                        "account": existing.to_dict()}), 200

    # Step 4: Save with Daya-native bank code and Paystack recipient_code
    is_first = VendorBankAccount.query.filter_by(vendor_id=vendor_id).count() == 0

    bank_account = VendorBankAccount(
        vendor_id=vendor_id,
        bank_name=bank_name,
        bank_code=bank_code,           # CBN/Daya code — works on Daya transfers
        account_number=account_number,
        account_name=account_name if verified else None,
        recipient_code=recipient_code, # Paystack recipient — works on Paystack transfers
        is_verified=verified,
        verified_at=_utcnow() if verified else None,
        is_default=is_first,
    )
    db.session.add(bank_account)
    db.session.flush()  # Get bank_account.id without committing yet

    # Step 5: Create Paystack subaccount for split payments
    # (This is best-effort — if it fails, Daya payout still works fine)
    try:
        from app.services.escrow.paystack_provider import create_paystack_subaccount
        from app.models.user import Storefront
        storefront = Storefront.query.filter_by(vendor_id=vendor_id).first()
        business_name = storefront.store_name if storefront else (account_name or "Siiqo seller")
        # Use Paystack's bank code if different from Daya's
        # For standard banks they're the same; for mobile money they differ.
        # We attempt with the Daya code — Paystack will accept most standard codes.
        sub_result = create_paystack_subaccount(
            business_name=business_name,
            bank_code=bank_code,
            account_number=account_number,
        )
        if sub_result.get("success"):
            bank_account.paystack_subaccount_code = sub_result["subaccount_code"]
            if storefront:
                storefront.paystack_subaccount_code = sub_result["subaccount_code"]
            logging.info("[DAYA BANK REG] Paystack subaccount %s created for vendor %s",
                         sub_result["subaccount_code"], vendor_id)
        else:
            logging.warning("[DAYA BANK REG] Paystack subaccount failed for vendor %s: %s",
                            vendor_id, sub_result.get("error_message"))
    except Exception as exc:
        logging.warning("[DAYA BANK REG] Paystack subaccount error for vendor %s: %s", vendor_id, exc)

    # Step 6: Create Flutterwave subaccount for split payments
    # Best-effort — if it fails, Paystack and Daya payouts still work.
    # Flutterwave subaccount is used when buyers pay via Flutterwave
    # (international cards, local cards). The RS_xxx ID is stored on the
    # bank account record and looked up at checkout time.
    try:
        from app.services.flutterwave_service import create_subaccount as _flw_create_sub, is_configured as _flw_configured
        if _flw_configured():
            from app.models.user import Storefront, User as _U
            # Storefront is already loaded above; safe to reuse
            _flw_business_name = (storefront.store_name if storefront else (account_name or "Siiqo seller"))
            _flw_phone = ""
            if storefront:
                try:
                    _vendor_user = db.session.get(_U, int(vendor_id))
                    _flw_phone = (_vendor_user.phone or "") if _vendor_user else ""
                except Exception:
                    pass

            flw_result = _flw_create_sub(
                business_name=_flw_business_name,
                # Flutterwave uses the same CBN bank code as Daya
                bank_code=bank_code,
                account_number=account_number,
                business_mobile=_flw_phone or "08012345678",
                business_email=(storefront.vendor.email if storefront and storefront.vendor else ""),
            )
            if flw_result.get("success"):
                bank_account.flw_subaccount_id = flw_result["subaccount_id"]
                logging.info(
                    "[DAYA BANK REG] Flutterwave subaccount %s created for vendor %s",
                    flw_result["subaccount_id"], vendor_id,
                )
            else:
                logging.warning(
                    "[DAYA BANK REG] Flutterwave subaccount creation failed for vendor %s: %s",
                    vendor_id, flw_result.get("error_message"),
                )
        else:
            logging.info("[DAYA BANK REG] Flutterwave not configured — skipping FLW subaccount creation")
    except Exception as _flw_exc:
        logging.warning(
            "[DAYA BANK REG] Flutterwave subaccount error for vendor %s: %s",
            vendor_id, _flw_exc,
        )

    db.session.commit()
    # Verification 2-3: a payout account in a different name opens a re-check.
    from app.services.verification.rechecks import check_after_change
    check_after_change(vendor_id)

    logging.info("[DAYA BANK REG] Vendor %s registered Daya bank: %s %s (%s) name %s",
                 vendor_id, bank_name, mask_account(account_number), bank_code,
                 "confirmed" if verified else "pending")

    # Auto-retry any pending stalled payouts for this vendor now that bank account is added
    try:
        from app.models.order import Order
        from app.routes.payments import _payout_vendor_via_daya

        # Find all orders where vendor had a stalled payout notification
        pending_payout_notifs = Notification.query.filter_by(
            user_id=vendor_id,
            title="Payout Pending — Add Bank Account",
        ).all()

        for notif in pending_payout_notifs:
            if not notif.order_id:
                continue
            # Check if this order was already successfully paid
            already_sent = Notification.query.filter_by(
                user_id=vendor_id,
                order_id=notif.order_id,
                title="Payment On Its Way",
            ).first()
            if already_sent:
                continue

            order_to_pay = db.session.get(Order, notif.order_id)
            if not order_to_pay or not order_to_pay.escrow:
                continue
            escrow_to_pay = order_to_pay.escrow
            # Only released money, and never twice: a PAYOUT debit means paid.
            if escrow_to_pay.status != 'RELEASED' or Ledger.query.filter_by(
                    vendor_id=order_to_pay.vendor_id, transaction_type='DEBIT',
                    reference_id=f"PAYOUT-{escrow_to_pay.transaction_number or order_to_pay.id}").first():
                continue

            logging.info(
                "[DAYA RETRY] Auto-retrying stalled payout for Order #%s after bank account added by vendor %s",
                order_to_pay.id, vendor_id,
            )
            if _payout_vendor_via_daya(order_to_pay, escrow_to_pay):
                # Paid out automatically: write the matching DEBIT so the
                # ledger credit can't also be withdrawn.
                from app.routes.escrow import _record_payout_debit, _vendor_net_amount
                _record_payout_debit(order_to_pay, escrow_to_pay,
                                     _vendor_net_amount(order_to_pay, escrow_to_pay))
        db.session.commit()
    except Exception as retry_err:
        db.session.rollback()
        logging.warning("[DAYA RETRY] Error auto-retrying stalled payouts: %s", retry_err)

    return jsonify({
        "status": "success",
        "message": (f"Bank account registered successfully — verified as {account_name}" if verified
                    else NAME_PENDING_MESSAGE),
        "account": bank_account.to_dict(),
    }), 201
