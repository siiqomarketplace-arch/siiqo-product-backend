"""
escrow_tasks.py — Background tasks for escrow management
Handles auto-release, dispute timeouts, and payment reminders
"""
import logging
logger = logging.getLogger(__name__)
from datetime import datetime, timezone, timedelta
from app.extensions import db
from app.models.escrow import EscrowTransaction, EscrowStatus
from app.models.order import Order
from app.models.finance import Ledger, Receipt
from app.models.communication import Notification


def utcnow():
    return datetime.now(timezone.utc)


def _credit_vendor_ledger(vendor_id: int, amount: float, reference_id: str, description: str):
    """Write a CREDIT entry to the vendor's ledger."""
    from sqlalchemy import func
    from app.models.finance import Ledger as L
    
    credits = db.session.query(func.sum(L.amount)).filter_by(
        vendor_id=vendor_id, transaction_type='CREDIT'
    ).scalar() or 0
    debits = db.session.query(func.sum(L.amount)).filter_by(
        vendor_id=vendor_id, transaction_type='DEBIT'
    ).scalar() or 0
    balance_after = float(credits) - float(debits) + amount

    db.session.add(Ledger(
        vendor_id=vendor_id,
        transaction_type='CREDIT',
        amount=amount,
        description=description,
        reference_id=reference_id,
        balance_after=balance_after,
    ))


def auto_release_escrow():
    """
    Auto-release escrow funds after a dynamic holding period based on vendor trust tier:
    PLATINUM: 24h, GOLD: 48h, SILVER: 72h, BRONZE: 96h.
    Run this task every hour via cron job or scheduler.
    """
    logger.info(f"[{utcnow()}] Running auto-release escrow task...")
    
    # Find all delivered escrow transactions and filter dynamically in python
    delivered_escrows = EscrowTransaction.query.filter(
        EscrowTransaction.status == EscrowStatus.DELIVERED
    ).all()
    
    escrows_to_release = []
    for escrow in delivered_escrows:
        order = escrow.order
        if not order:
            continue
            
        try:
            from app.services.trust import get_vendor_auto_release_hours
            release_hours = get_vendor_auto_release_hours(order.vendor_id)
        except Exception as e:
            logger.error(f"Failed to fetch trust profile for vendor {order.vendor_id}: {e}. Falling back to 72 hours.")
            release_hours = 72
            
        cutoff_time = utcnow() - timedelta(hours=release_hours)
        if escrow.updated_at <= cutoff_time:
            escrows_to_release.append(escrow)
    
    released_count = 0
    
    for escrow in escrows_to_release:
        try:
            order = escrow.order
            if not order:
                continue

            # Call PayScrow applycode to actually move the money before updating DB
            if escrow.payscrow_transaction_id and escrow.escrow_code:
                raw_code = str(escrow.escrow_code).strip()
                code_is_real = raw_code.isdigit() and 4 <= len(raw_code) <= 10
                if code_is_real:
                    import os, requests as _req
                    payscrow_key = os.environ.get('PAYSCROW_API_KEY', '')
                    base_url = os.environ.get('PAYSCROW_BASE_URL')
                    if not base_url:
                        is_sandbox = (
                            not payscrow_key
                            or payscrow_key.startswith('ps_9')
                            or os.environ.get('PAYSCROW_ENV', '').lower() == 'sandbox'
                        )
                        base_url = "https://api.payscrow.dev" if is_sandbox else "https://api.payscrow.net"
                    headers = {"BrokerApiKey": payscrow_key, "Content-Type": "application/json"}
                    try:
                        resp = _req.post(
                            f"{base_url}/api/v3/escrow/escrowtransactions/applycode",
                            json={
                                "transactionId": escrow.payscrow_transaction_id,
                                "code": raw_code,
                            },
                            headers=headers,
                            timeout=15,
                        )
                        resp_data = resp.json()
                        if not resp_data.get('success'):
                            logger.warning(
                                f"  ⚠ PayScrow applycode non-success for ESC {escrow.transaction_number}: {resp.text}"
                            )
                    except Exception as api_err:
                        logger.warning(
                            f"  ⚠ PayScrow applycode unreachable for ESC {escrow.transaction_number}: {api_err} "
                            "— releasing internally"
                        )
                else:
                    logger.info(
                        f"  ⚠ Escrow code '{raw_code[:40]}' for ESC {escrow.transaction_number} is not numeric "
                        "— releasing internally (sandbox placeholder or email message)"
                    )
            else:
                logger.warning(
                    f"  ⚠ No payscrow_transaction_id/escrow_code for ESC {escrow.transaction_number} — "
                    "releasing internally."
                )

            # ── Trigger vendor payout based on payment method ────────────────
            is_crypto_order = (order.payment_method or '').upper() == 'CRYPTO'
            is_paystack_order = (order.payment_method or '').upper() == 'PAYSTACK' or (
                not is_crypto_order and escrow.payscrow_transaction_id
                and (escrow.payscrow_transaction_id.startswith('ORD-'))
            )

            if is_crypto_order:
                # Funds in Daya collection balance — pay out via Daya
                try:
                    from app.routes.payments import _payout_vendor_via_daya
                    _payout_vendor_via_daya(order, escrow)
                    logger.info(
                        f"  [DAYA AUTO-RELEASE] Payout initiated for Order #{order.id}"
                    )
                except Exception as daya_err:
                    logger.error(
                        f"  [DAYA AUTO-RELEASE] Payout failed for Order #{order.id}: {daya_err}"
                    )
            elif is_paystack_order:
                # Paystack split payment — vendor was already paid at checkout.
                # No PayScrow applycode, no manual transfer needed.
                # Just release the DB record and credit the ledger for display.
                logger.info(
                    f"  [PAYSTACK AUTO-RELEASE] Order #{order.id} — "
                    "vendor paid via split at checkout, releasing escrow record only."
                )
            else:
                # Legacy PayScrow orders (Payment Links only)
                if escrow.payscrow_transaction_id and escrow.escrow_code:
                    raw_code = str(escrow.escrow_code).strip()
                    code_is_real = raw_code.isdigit() and 4 <= len(raw_code) <= 10
                    if code_is_real:
                        import os, requests as _req
                        payscrow_key = os.environ.get('PAYSCROW_API_KEY', '')
                        base_url = os.environ.get('PAYSCROW_BASE_URL')
                        if not base_url:
                            is_sandbox = (
                                not payscrow_key
                                or payscrow_key.startswith('ps_9')
                                or os.environ.get('PAYSCROW_ENV', '').lower() == 'sandbox'
                            )
                            base_url = "https://api.payscrow.dev" if is_sandbox else "https://api.payscrow.net"
                        headers = {"BrokerApiKey": payscrow_key, "Content-Type": "application/json"}
                        try:
                            resp = _req.post(
                                f"{base_url}/api/v3/escrow/escrowtransactions/applycode",
                                json={
                                    "transactionId": escrow.payscrow_transaction_id,
                                    "code": raw_code,
                                },
                                headers=headers,
                                timeout=15,
                            )
                            resp_data = resp.json()
                            if not resp_data.get('success'):
                                logger.warning(
                                    f"  ⚠ PayScrow applycode non-success for ESC {escrow.transaction_number}: {resp.text}"
                                )
                        except Exception as api_err:
                            logger.warning(
                                f"  ⚠ PayScrow applycode unreachable for ESC {escrow.transaction_number}: {api_err} "
                                "— releasing internally"
                            )
                    else:
                        logger.info(
                            f"  ⚠ Escrow code '{raw_code[:40]}' for ESC {escrow.transaction_number} is not numeric "
                            "— releasing internally"
                        )
            escrow.status = EscrowStatus.RELEASED
            escrow.released_at = utcnow()
            order.status = 'COMPLETED'

            # Credit vendor ledger (net of fee)
            net_amount = float(escrow.amount) - float(escrow.fee_amount or 0)
            _credit_vendor_ledger(
                vendor_id=order.vendor_id,
                amount=net_amount,
                reference_id=escrow.transaction_number,
                description=f"Auto-released payout for Order #{order.id}",
            )
            
            # Create Receipt
            existing_receipt = Receipt.query.filter_by(order_id=order.id).first()
            if not existing_receipt:
                db.session.add(Receipt(order_id=order.id))
            
            # Notify vendor
            db.session.add(Notification(
                user_id=order.vendor_id,
                title="Funds Auto-Released",
                message=f"₦{net_amount:,.2f} has been credited to your ledger for Order #{order.id} (auto-released after 72 hours).",
                type="ESCROW",
                order_id=order.id,
            ))
            
            # Notify buyer
            db.session.add(Notification(
                user_id=order.buyer_id,
                title="Order Complete",
                message=f"Order #{order.id} is complete. Funds have been released to the vendor. Thank you for shopping on Siiqo!",
                type="ORDER",
                order_id=order.id,
            ))
            
            db.session.commit()
            
            # Send email notifications (non-blocking)
            from app.utils.email import send_siiqo_email
            from app.models.user import User

            vendor = db.session.get(User, order.vendor_id)
            if vendor and vendor.email:
                try:
                    send_siiqo_email(
                        to_email=vendor.email,
                        subject="Siiqo - Payout Released",
                        template_name="system_notice",
                        first_name=vendor.first_name or "Vendor",
                        notice_text=f"Congratulations! Payout of ₦{net_amount:,.2f} has been released to your Siiqo wallet for Order #{order.id} (auto-released after 72 hours)."
                    )
                except Exception as e:
                    logger.warning(f"[EMAIL WARN] Failed to send payout release email to vendor: {e}")

            buyer = db.session.get(User, order.buyer_id)
            if buyer and buyer.email:
                try:
                    send_siiqo_email(
                        to_email=buyer.email,
                        subject="Siiqo - Order Completed",
                        template_name="system_notice",
                        first_name=buyer.first_name or "Buyer",
                        notice_text=f"Thank you! Order #{order.id} is now complete. The funds have been released to the vendor. We hope you enjoyed shopping on Siiqo!"
                    )
                except Exception as e:
                    logger.warning(f"[EMAIL WARN] Failed to send order completed email to buyer: {e}")

            released_count += 1
            
            logger.info(f"  ✓ Auto-released escrow {escrow.transaction_number} for Order #{order.id}")
            
        except Exception as e:
            db.session.rollback()
            logger.info(f"  ✗ Error auto-releasing escrow {escrow.transaction_number}: {e}")
            continue
    
    logger.info(f"[{utcnow()}] Auto-release task completed. Released {released_count} escrow(s).")
    return released_count


def send_delivery_reminders():
    """
    Send reminders to buyers to confirm delivery.
    Run this task daily.
    """
    logger.info(f"[{utcnow()}] Running delivery reminder task...")
    
    # Find all delivered escrow transactions and filter dynamically based on release thresholds
    delivered_escrows = EscrowTransaction.query.filter(
        EscrowTransaction.status == EscrowStatus.DELIVERED
    ).all()
    
    escrows_to_remind = []
    for escrow in delivered_escrows:
        order = escrow.order
        if not order:
            continue
            
        try:
            from app.services.trust import get_vendor_auto_release_hours
            release_hours = get_vendor_auto_release_hours(order.vendor_id)
        except Exception:
            release_hours = 72
            
        # Send reminder at dynamic timing (e.g. 12h for Platinum, 24h for Gold, 48h for Silver/Bronze)
        reminder_trigger_hours = max(12, release_hours - 24)
        
        cutoff_time = utcnow() - timedelta(hours=reminder_trigger_hours)
        release_cutoff = utcnow() - timedelta(hours=release_hours)
        
        if escrow.updated_at <= cutoff_time and escrow.updated_at > release_cutoff:
            escrows_to_remind.append(escrow)
    
    reminded_count = 0
    
    for escrow in escrows_to_remind:
        try:
            order = escrow.order
            if not order:
                continue
            
            # Check if we already sent a reminder (avoid spam)
            recent_reminder = Notification.query.filter(
                Notification.user_id == order.buyer_id,
                Notification.order_id == order.id,
                Notification.title == "Confirm Your Delivery",
                Notification.created_at >= utcnow() - timedelta(hours=24)
            ).first()
            
            if recent_reminder:
                continue  # Already reminded in last 24 hours
            
            # Send reminder
            db.session.add(Notification(
                user_id=order.buyer_id,
                title="Confirm Your Delivery",
                message=f"Please confirm delivery of Order #{order.id}. Funds will be auto-released in 24 hours if not confirmed.",
                type="ORDER",
                order_id=order.id,
            ))
            
            db.session.commit()
            reminded_count += 1
            
            logger.info(f"  ✓ Sent delivery reminder for Order #{order.id}")
            
        except Exception as e:
            db.session.rollback()
            logger.info(f"  ✗ Error sending reminder for escrow {escrow.transaction_number}: {e}")
            continue
    
    logger.info(f"[{utcnow()}] Delivery reminder task completed. Sent {reminded_count} reminder(s).")
    return reminded_count


def check_pending_payments():
    """
    Check for orders stuck in PENDING_PAYMENT for more than 24 hours and cancel them.
    Run this task daily.
    """
    logger.info(f"[{utcnow()}] Running pending payment check task...")
    
    # Find escrow transactions that are PENDING_PAYMENT for more than 1 hour
    cutoff_time = utcnow() - timedelta(hours=1)
    
    stale_escrows = EscrowTransaction.query.filter(
        EscrowTransaction.status == EscrowStatus.PENDING_PAYMENT,
        EscrowTransaction.created_at <= cutoff_time
    ).all()
    
    cancelled_count = 0
    
    for escrow in stale_escrows:
        try:
            order = escrow.order
            if not order:
                continue
            
            # Restore stock quantity for the items in the cancelled order
            for item in order.items:
                if item.product:
                    item.product.stock_quantity = (item.product.stock_quantity or 0) + item.quantity

            # Cancel order and escrow
            escrow.status = EscrowStatus.CANCELLED
            order.status = 'CANCELLED'
            
            # Notify buyer
            db.session.add(Notification(
                user_id=order.buyer_id,
                title="Order Cancelled",
                message=f"Order #{order.id} was cancelled due to non-payment within 1 hour.",
                type="ORDER",
                order_id=order.id,
            ))
            
            db.session.commit()
            cancelled_count += 1
            
            logger.info(f"  ✓ Cancelled stale order #{order.id}")
            
        except Exception as e:
            db.session.rollback()
            logger.info(f"  ✗ Error cancelling escrow {escrow.transaction_number}: {e}")
            continue
    
    logger.info(f"[{utcnow()}] Pending payment check completed. Cancelled {cancelled_count} order(s).")
    return cancelled_count


# Main task runner (call this from cron job or scheduler)
def run_all_escrow_tasks():
    """Run all escrow background tasks"""
    logger.info("=" * 60)
    logger.info("ESCROW BACKGROUND TASKS")
    logger.info("=" * 60)
    
    auto_release_escrow()
    send_delivery_reminders()
    check_pending_payments()
    
    logger.info("=" * 60)
    logger.info("ALL TASKS COMPLETED")
    logger.info("=" * 60)


if __name__ == '__main__':
    # For testing: python -m app.tasks.escrow_tasks
    from app import create_app
    app = create_app()
    with app.app_context():
        run_all_escrow_tasks()
