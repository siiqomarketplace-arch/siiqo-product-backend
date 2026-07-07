import logging
"""
withdrawal.py — Vendor Withdrawal Routes
Handles bank accounts, withdrawal requests, and POD payments
"""
import os
import requests
from datetime import datetime, timezone
from decimal import Decimal

from flask import Blueprint, request, jsonify
from flask_jwt_extended import jwt_required, get_jwt_identity
from sqlalchemy import func

from app.extensions import db
from app.models.withdrawal import VendorBankAccount, Withdrawal, PODPayment, PartnerBankAccount, PartnerWithdrawal
from app.models.order import Order
from app.models.finance import Ledger
from app.models.communication import Notification
from app.models.user import User

withdrawal_bp = Blueprint('withdrawal', __name__)

PAYSTACK_SECRET_KEY = os.environ.get('PAYSTACK_SECRET_KEY', '')
PAYSTACK_BASE_URL = 'https://api.paystack.co'


def _utcnow():
    return datetime.now(timezone.utc)


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


# ---------------------------------------------------------------------------
# BANK ACCOUNT MANAGEMENT
# ---------------------------------------------------------------------------

@withdrawal_bp.route('/bank-accounts', methods=['GET'])
@jwt_required()
def get_bank_accounts():
    """Get vendor's bank accounts"""
    vendor_id = get_jwt_identity()
    accounts = VendorBankAccount.query.filter_by(vendor_id=vendor_id).all()
    return jsonify({
        'status': 'success',
        'accounts': [acc.to_dict() for acc in accounts]
    }), 200


@withdrawal_bp.route('/bank-accounts', methods=['POST'])
@jwt_required()
def add_bank_account():
    """Add and verify a new bank account"""
    vendor_id = get_jwt_identity()
    data = request.get_json() or {}
    
    bank_code = data.get('bank_code')
    account_number = data.get('account_number')
    
    if not bank_code or not account_number:
        return jsonify({'message': 'bank_code and account_number required'}), 400
    
    # Verify account with Paystack
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
        existing = VendorBankAccount.query.filter_by(
            vendor_id=vendor_id,
            account_number=account_number,
            bank_code=bank_code
        ).first()
        
        if existing:
            return jsonify({'message': 'This bank account is already added'}), 400
        
        # 4. Create bank account record
        is_first = VendorBankAccount.query.filter_by(vendor_id=vendor_id).count() == 0
        
        bank_account = VendorBankAccount(
            vendor_id=vendor_id,
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
        db.session.flush()  # Get bank_account.id without committing yet

        # ── Create Paystack subaccount for Split Payments ──────────────────
        # Register the vendor's bank account as a Paystack subaccount so
        # digital/service checkouts can split the payment natively.
        try:
            from app.services.escrow.paystack_provider import create_paystack_subaccount
            from app.models.user import Storefront
            storefront = Storefront.query.filter_by(vendor_id=vendor_id).first()
            business_name = (storefront.store_name if storefront else account_name)
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



@withdrawal_bp.route('/bank-accounts/<int:account_id>/set-default', methods=['PATCH'])
@jwt_required()
def set_default_account(account_id):
    """Set a bank account as default"""
    vendor_id = get_jwt_identity()
    
    account = db.session.get(VendorBankAccount, account_id)
    if not account or account.vendor_id != int(vendor_id):
        return jsonify({'message': 'Account not found'}), 404
    
    # Remove default from all accounts
    VendorBankAccount.query.filter_by(vendor_id=vendor_id).update({'is_default': False})
    
    # Set this as default
    account.is_default = True
    db.session.commit()
    
    return jsonify({
        'status': 'success',
        'message': 'Default account updated'
    }), 200


@withdrawal_bp.route('/bank-accounts/<int:account_id>', methods=['DELETE'])
@jwt_required()
def delete_bank_account(account_id):
    """Delete a bank account"""
    vendor_id = get_jwt_identity()
    
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
    
    return jsonify({
        'status': 'success',
        'message': 'Bank account deleted'
    }), 200


# ---------------------------------------------------------------------------
# WITHDRAWAL REQUESTS
# ---------------------------------------------------------------------------

@withdrawal_bp.route('/balance', methods=['GET'])
@jwt_required()
def get_balance():
    """Get vendor's available balance"""
    vendor_id = get_jwt_identity()
    balance = _get_ledger_balance(vendor_id)
    
    # Get pending withdrawals
    pending_amount = db.session.query(func.sum(Withdrawal.amount)).filter_by(
        vendor_id=vendor_id,
        status='PENDING'
    ).scalar() or Decimal('0')
    
    available = balance - Decimal(str(pending_amount))
    
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
    vendor_id = get_jwt_identity()
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
    provider = os.environ.get("ACTIVE_ESCROW_PROVIDER", "payscrow").lower()

    if provider != "paystack":
        return jsonify({
            'message': (
                'Manual withdrawals are currently deactivated. '
                'Escrow payouts are processed automatically upon delivery confirmation.'
            )
        }), 400

    vendor_id = get_jwt_identity()

    # Check available balance
    balance = _get_ledger_balance(int(vendor_id))
    pending_amount = db.session.query(func.sum(Withdrawal.amount)).filter_by(
        vendor_id=vendor_id,
        status='PENDING',
    ).scalar() or Decimal('0')
    available = balance - Decimal(str(pending_amount))

    MIN_WITHDRAWAL = Decimal('1000')  # ₦1,000 minimum
    if available < MIN_WITHDRAWAL:
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

    if not bank_acc or not bank_acc.recipient_code:
        return jsonify({
            'message': 'No verified bank account found. '
                       'Please add and verify a bank account in your payout settings first.'
        }), 400

    data = request.get_json() or {}
    requested_amount = data.get('amount')
    if requested_amount:
        try:
            requested_amount = Decimal(str(requested_amount))
            if requested_amount <= 0 or requested_amount > available:
                return jsonify({'message': 'Invalid withdrawal amount.'}), 400
        except Exception:
            return jsonify({'message': 'Invalid amount format.'}), 400
    else:
        requested_amount = available  # default: withdraw all

    # Paystack transfer fee is ₦50 flat (waived above ₦5,000 on live tier)
    # We absorb this — vendor receives full requested amount
    import uuid as _uuid
    reference = f"WD-{_uuid.uuid4().hex[:12].upper()}"

    from app.services.escrow.paystack_provider import paystack_transfer_to_vendor
    result = paystack_transfer_to_vendor(
        recipient_code=bank_acc.recipient_code,
        amount_ngn=float(requested_amount),
        reference=reference,
        reason="Siiqo vendor withdrawal",
    )

    if not result.get("success"):
        return jsonify({
            'message': f"Withdrawal failed: {result.get('error_message', 'Unknown error.')}"
        }), 400

    # Record withdrawal + debit ledger
    transfer_code = result.get("transfer_code", "")
    new_withdrawal = Withdrawal(
        vendor_id=int(vendor_id),
        amount=requested_amount,
        fee_amount=Decimal('0'),
        net_amount=requested_amount,
        status='PENDING',
        bank_account_id=bank_acc.id,
        transfer_code=transfer_code,
        transfer_reference=reference,
    )
    db.session.add(new_withdrawal)

    _debit_ledger(
        vendor_id=int(vendor_id),
        amount=requested_amount,
        description=f"Withdrawal to {bank_acc.bank_name} {bank_acc.account_number[-4:]}",
        reference_id=reference,
    )

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
    vendor_id = get_jwt_identity()
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
    vendor_id = get_jwt_identity()
    data = request.get_json() or {}
    
    # Get order
    order = db.session.get(Order, order_id)
    if not order:
        return jsonify({'message': 'Order not found'}), 404
    
    if order.vendor_id != int(vendor_id):
        return jsonify({'message': 'Unauthorized'}), 403
    
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
    
    # Credit vendor ledger (no platform fee for POD)
    from app.routes.escrow import _credit_vendor_ledger
    _credit_vendor_ledger(
        vendor_id=vendor_id,
        amount=float(order.total_amount),
        reference_id=f'POD-{order.id}',
        description=f'POD payment for Order #{order.id}'
    )
    
    # Notify buyer
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
    vendor_id = get_jwt_identity()
    
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
            params={'country': 'nigeria'},
            timeout=10
        )
        
        data = response.json()
        
        if data.get('status'):
            banks = data['data']
            # Sort by name
            banks.sort(key=lambda x: x['name'])
            return jsonify({
                'status': 'success',
                'banks': banks
            }), 200
        else:
            return jsonify({'message': 'Could not fetch banks'}), 500
            
    except Exception as e:
        return jsonify({'message': f'Error: {str(e)}'}), 500


@withdrawal_bp.route('/banks/resolve', methods=['GET'])
def resolve_bank_account():
    """
    Resolve (verify) a bank account name — public endpoint.
    Used by the partner apply form and vendor onboarding to auto-fill account name.
    Query params: bank_code, account_number
    """
    bank_code      = (request.args.get('bank_code') or '').strip()
    account_number = (request.args.get('account_number') or '').strip()

    if not bank_code or not account_number:
        return jsonify({'message': 'bank_code and account_number are required'}), 400

    if len(account_number) != 10:
        return jsonify({'message': 'Account number must be exactly 10 digits'}), 400

    try:
        headers = {
            'Authorization': f'Bearer {PAYSTACK_SECRET_KEY}',
            'Content-Type': 'application/json',
        }
        resp = requests.get(
            f'{PAYSTACK_BASE_URL}/bank/resolve',
            headers=headers,
            params={'account_number': account_number, 'bank_code': bank_code},
            timeout=10,
        )
        data = resp.json()

        if not data.get('status'):
            return jsonify({
                'message': data.get('message', 'Could not verify account. Check details.'),
                'account_name': None,
            }), 400

        return jsonify({
            'status': 'success',
            'account_name': data['data']['account_name'],
            'account_number': data['data']['account_number'],
        }), 200

    except requests.exceptions.RequestException as e:
        return jsonify({'message': f'Verification failed: {str(e)}'}), 500
    except Exception as e:
        return jsonify({'message': f'Error: {str(e)}'}), 500


# ---------------------------------------------------------------------------
# LOGISTICS PARTNER BANK ACCOUNT MANAGEMENT & WITHDRAWALS
# ---------------------------------------------------------------------------

@withdrawal_bp.route('/partner/bank-accounts', methods=['GET'])
@jwt_required()
def get_partner_bank_accounts():
    """Get partner's bank accounts"""
    partner_id = get_jwt_identity()
    accounts = PartnerBankAccount.query.filter_by(partner_id=partner_id).all()
    return jsonify({
        'status': 'success',
        'accounts': [acc.to_dict() for acc in accounts]
    }), 200


@withdrawal_bp.route('/partner/bank-accounts', methods=['POST'])
@jwt_required()
def add_partner_bank_account():
    """Add and verify a new partner bank account"""
    partner_id = get_jwt_identity()
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
def set_default_partner_account(account_id):
    """Set a partner bank account as default"""
    partner_id = get_jwt_identity()
    
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
def delete_partner_bank_account(account_id):
    """Delete a partner bank account"""
    partner_id = get_jwt_identity()
    
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
def get_partner_balance():
    """Get logistics partner's available balance"""
    partner_id = get_jwt_identity()
    
    from app.models.escrow import LogisticsAssignment
    from app.models.withdrawal import PartnerWithdrawal
    
    total_earned = db.session.query(func.sum(LogisticsAssignment.delivery_fee)).filter_by(
        partner_id=partner_id, status='DELIVERED'
    ).scalar() or Decimal('0')
    
    # Sum of successfully completed or pending withdrawals
    withdrawn_amount = db.session.query(func.sum(PartnerWithdrawal.amount)).filter(
        PartnerWithdrawal.partner_id == partner_id,
        PartnerWithdrawal.status.in_(['PENDING', 'PROCESSING', 'COMPLETED'])
    ).scalar() or Decimal('0')
    
    available = Decimal(str(total_earned)) - Decimal(str(withdrawn_amount))
    
    return jsonify({
        'status': 'success',
        'balance': str(total_earned),
        'pending_withdrawals': str(withdrawn_amount),
        'available': str(available),
        'currency': 'NGN'
    }), 200


@withdrawal_bp.route('/partner/withdrawals', methods=['GET'])
@jwt_required()
def get_partner_withdrawals():
    """Get partner's withdrawal history"""
    partner_id = get_jwt_identity()
    page = int(request.args.get('page', 1))
    per_page = min(int(request.args.get('per_page', 20)), 100)
    
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


@withdrawal_bp.route('/partner/withdrawals', methods=['POST'])
@jwt_required()
def request_partner_withdrawal():
    """Partner requests a withdrawal. Triggers a Paystack transfer."""
    partner_id = get_jwt_identity()

    # Calculate available balance
    from app.models.escrow import LogisticsAssignment
    from app.models.withdrawal import PartnerWithdrawal
    
    total_earned = db.session.query(func.sum(LogisticsAssignment.delivery_fee)).filter_by(
        partner_id=partner_id, status='DELIVERED'
    ).scalar() or Decimal('0')
    
    withdrawn_amount = db.session.query(func.sum(PartnerWithdrawal.amount)).filter(
        PartnerWithdrawal.partner_id == partner_id,
        PartnerWithdrawal.status.in_(['PENDING', 'PROCESSING', 'COMPLETED'])
    ).scalar() or Decimal('0')
    
    available = Decimal(str(total_earned)) - Decimal(str(withdrawn_amount))

    MIN_WITHDRAWAL = Decimal('1000')  # ₦1,000 minimum
    if available < MIN_WITHDRAWAL:
        return jsonify({
            'message': f'Insufficient balance. Minimum withdrawal is ₦{MIN_WITHDRAWAL:,.2f}. '
                       f'Your available balance is ₦{available:,.2f}.'
        }), 400

    # Get default bank account
    bank_acc = PartnerBankAccount.query.filter_by(
        partner_id=partner_id, is_default=True
    ).first()
    if not bank_acc:
        bank_acc = PartnerBankAccount.query.filter_by(partner_id=partner_id).first()

    if not bank_acc or not bank_acc.recipient_code:
        return jsonify({
            'message': 'No verified bank account found. '
                       'Please add and verify a bank account in your payout settings first.'
        }), 400

    data = request.get_json() or {}
    requested_amount = data.get('amount')
    if requested_amount:
        try:
            requested_amount = Decimal(str(requested_amount))
            if requested_amount <= 0 or requested_amount > available:
                return jsonify({'message': 'Invalid withdrawal amount.'}), 400
        except Exception:
            return jsonify({'message': 'Invalid amount format.'}), 400
    else:
        requested_amount = available  # withdraw all

    import uuid as _uuid
    reference = f"PWD-{_uuid.uuid4().hex[:12].upper()}"

    from app.services.escrow.paystack_provider import paystack_transfer_to_vendor
    result = paystack_transfer_to_vendor(
        recipient_code=bank_acc.recipient_code,
        amount_ngn=float(requested_amount),
        reference=reference,
        reason="Siiqo partner withdrawal",
    )

    if not result.get("success"):
        return jsonify({
            'message': f"Withdrawal failed: {result.get('error_message', 'Unknown error.')}"
        }), 400

    # Record withdrawal
    transfer_code = result.get("transfer_code", "")
    new_withdrawal = PartnerWithdrawal(
        partner_id=int(partner_id),
        amount=requested_amount,
        fee_amount=Decimal('0'),
        net_amount=requested_amount,
        status='PENDING',
        bank_account_id=bank_acc.id,
        transfer_code=transfer_code,
        transfer_reference=reference,
    )
    db.session.add(new_withdrawal)
    db.session.commit()

    return jsonify({
        'status': 'success',
        'message': 'Withdrawal initiated. Funds will arrive within 24 hours.',
        'transfer_code': transfer_code,
        'amount': str(requested_amount),
    }), 200

