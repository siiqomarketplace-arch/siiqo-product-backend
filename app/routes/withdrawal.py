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
from app.models.withdrawal import VendorBankAccount, Withdrawal, PODPayment
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
    """Request a withdrawal"""
    vendor_id = get_jwt_identity()
    data = request.get_json() or {}
    
    amount = data.get('amount')
    bank_account_id = data.get('bank_account_id')
    
    if not amount:
        return jsonify({'message': 'amount is required'}), 400
    
    try:
        amount = Decimal(str(amount))
    except:
        return jsonify({'message': 'Invalid amount'}), 400
    
    # Minimum withdrawal
    if amount < Decimal('1000'):
        return jsonify({'message': 'Minimum withdrawal is ₦1,000'}), 400
    
    # Acquire row-level lock to prevent concurrent withdrawals race condition
    user_lock = db.session.query(User).with_for_update().get(vendor_id)
    if not user_lock:
        return jsonify({'message': 'Vendor not found'}), 404

    # Check balance
    balance = _get_ledger_balance(vendor_id)
    pending_amount = db.session.query(func.sum(Withdrawal.amount)).filter_by(
        vendor_id=vendor_id,
        status='PENDING'
    ).scalar() or Decimal('0')
    
    available = balance - Decimal(str(pending_amount))
    
    if amount > available:
        return jsonify({
            'message': f'Insufficient balance. Available: ₦{available:,.2f}'
        }), 400
    
    # Get bank account
    if bank_account_id:
        bank_account = db.session.get(VendorBankAccount, bank_account_id)
        if not bank_account or bank_account.vendor_id != int(vendor_id):
            return jsonify({'message': 'Invalid bank account'}), 400
    else:
        # Use default account
        bank_account = VendorBankAccount.query.filter_by(
            vendor_id=vendor_id,
            is_default=True
        ).first()
        
        if not bank_account:
            return jsonify({'message': 'No default bank account found. Please add one.'}), 400
    
    # Calculate fee and net amount
    fee_amount = Decimal('50.00')  # Paystack transfer fee
    net_amount = amount - fee_amount
    
    # Create withdrawal request
    withdrawal = Withdrawal(
        vendor_id=vendor_id,
        bank_account_id=bank_account.id,
        amount=amount,
        fee_amount=fee_amount,
        net_amount=net_amount,
        status='PENDING'
    )
    
    db.session.add(withdrawal)
    db.session.commit()
    
    # Notify vendor
    db.session.add(Notification(
        user_id=vendor_id,
        title='Withdrawal Request Received',
        message=f'Your withdrawal request for ₦{amount:,.2f} is being processed.',
        type='WITHDRAWAL'
    ))
    db.session.commit()
    
    # Process withdrawal immediately (in production, use background task)
    try:
        _process_withdrawal(withdrawal.id)
    except Exception as e:
        print(f"Withdrawal processing error: {e}")
        # Don't fail the request, it will be retried
    
    return jsonify({
        'status': 'success',
        'message': 'Withdrawal request submitted successfully',
        'withdrawal': withdrawal.to_dict()
    }), 201


def _process_withdrawal(withdrawal_id: int):
    """Process a withdrawal via Paystack Transfer API"""
    withdrawal = db.session.get(Withdrawal, withdrawal_id)
    if not withdrawal or withdrawal.status != 'PENDING':
        return
    
    withdrawal.status = 'PROCESSING'
    db.session.commit()
    
    try:
        headers = {
            'Authorization': f'Bearer {PAYSTACK_SECRET_KEY}',
            'Content-Type': 'application/json'
        }
        
        # Initiate transfer
        transfer_url = f'{PAYSTACK_BASE_URL}/transfer'
        transfer_payload = {
            'source': 'balance',
            'amount': int(withdrawal.net_amount * 100),  # Convert to kobo
            'recipient': withdrawal.bank_account.recipient_code,
            'reason': f'Siiqo payout - {withdrawal.withdrawal_number}',
            'reference': withdrawal.withdrawal_number
        }
        
        response = requests.post(transfer_url, headers=headers, json=transfer_payload, timeout=15)
        response_data = response.json()
        
        if response_data.get('status'):
            # Transfer initiated successfully
            withdrawal.transfer_code = response_data['data'].get('transfer_code')
            withdrawal.transfer_reference = response_data['data'].get('reference')
            withdrawal.status = 'COMPLETED'
            withdrawal.completed_at = _utcnow()
            
            # Debit ledger
            _debit_ledger(
                vendor_id=withdrawal.vendor_id,
                amount=withdrawal.amount,
                description=f'Withdrawal to bank account - {withdrawal.withdrawal_number}',
                reference_id=withdrawal.withdrawal_number
            )
            
            # Notify vendor
            db.session.add(Notification(
                user_id=withdrawal.vendor_id,
                title='Withdrawal Completed',
                message=f'₦{withdrawal.net_amount:,.2f} has been sent to your bank account.',
                type='WITHDRAWAL'
            ))
            
        else:
            # Transfer failed
            withdrawal.status = 'FAILED'
            withdrawal.failure_reason = response_data.get('message', 'Transfer failed')
            withdrawal.retry_count += 1
            
            # Notify vendor
            db.session.add(Notification(
                user_id=withdrawal.vendor_id,
                title='Withdrawal Failed',
                message=f'Your withdrawal request failed: {withdrawal.failure_reason}',
                type='WITHDRAWAL'
            ))
        
        db.session.commit()
        
    except Exception as e:
        withdrawal.status = 'FAILED'
        withdrawal.failure_reason = str(e)
        withdrawal.retry_count += 1
        db.session.commit()


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
@jwt_required()
def get_banks():
    """Get list of Nigerian banks from Paystack"""
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
