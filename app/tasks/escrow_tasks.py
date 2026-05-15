"""
escrow_tasks.py — Background tasks for escrow management
Handles auto-release, dispute timeouts, and payment reminders
"""
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
    Auto-release escrow funds after 72 hours of delivery confirmation.
    Run this task every hour via cron job or scheduler.
    """
    print(f"[{utcnow()}] Running auto-release escrow task...")
    
    # Find escrow transactions that are DELIVERED and older than 72 hours
    cutoff_time = utcnow() - timedelta(hours=72)
    
    escrows_to_release = EscrowTransaction.query.filter(
        EscrowTransaction.status == EscrowStatus.DELIVERED,
        EscrowTransaction.updated_at <= cutoff_time
    ).all()
    
    released_count = 0
    
    for escrow in escrows_to_release:
        try:
            order = escrow.order
            if not order:
                continue
            
            # Release funds
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
            released_count += 1
            
            print(f"  ✓ Auto-released escrow {escrow.transaction_number} for Order #{order.id}")
            
        except Exception as e:
            db.session.rollback()
            print(f"  ✗ Error auto-releasing escrow {escrow.transaction_number}: {e}")
            continue
    
    print(f"[{utcnow()}] Auto-release task completed. Released {released_count} escrow(s).")
    return released_count


def send_delivery_reminders():
    """
    Send reminders to buyers to confirm delivery.
    Run this task daily.
    """
    print(f"[{utcnow()}] Running delivery reminder task...")
    
    # Find escrow transactions that are DELIVERED for 48 hours but not released
    cutoff_time = utcnow() - timedelta(hours=48)
    reminder_cutoff = utcnow() - timedelta(hours=72)  # Don't remind if auto-release is imminent
    
    escrows_to_remind = EscrowTransaction.query.filter(
        EscrowTransaction.status == EscrowStatus.DELIVERED,
        EscrowTransaction.updated_at <= cutoff_time,
        EscrowTransaction.updated_at > reminder_cutoff
    ).all()
    
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
            
            print(f"  ✓ Sent delivery reminder for Order #{order.id}")
            
        except Exception as e:
            db.session.rollback()
            print(f"  ✗ Error sending reminder for escrow {escrow.transaction_number}: {e}")
            continue
    
    print(f"[{utcnow()}] Delivery reminder task completed. Sent {reminded_count} reminder(s).")
    return reminded_count


def check_pending_payments():
    """
    Check for orders stuck in PENDING_PAYMENT for more than 24 hours and cancel them.
    Run this task daily.
    """
    print(f"[{utcnow()}] Running pending payment check task...")
    
    # Find escrow transactions that are PENDING_PAYMENT for more than 24 hours
    cutoff_time = utcnow() - timedelta(hours=24)
    
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
            
            # Cancel order and escrow
            escrow.status = EscrowStatus.CANCELLED
            order.status = 'CANCELLED'
            
            # Notify buyer
            db.session.add(Notification(
                user_id=order.buyer_id,
                title="Order Cancelled",
                message=f"Order #{order.id} was cancelled due to non-payment within 24 hours.",
                type="ORDER",
                order_id=order.id,
            ))
            
            db.session.commit()
            cancelled_count += 1
            
            print(f"  ✓ Cancelled stale order #{order.id}")
            
        except Exception as e:
            db.session.rollback()
            print(f"  ✗ Error cancelling escrow {escrow.transaction_number}: {e}")
            continue
    
    print(f"[{utcnow()}] Pending payment check completed. Cancelled {cancelled_count} order(s).")
    return cancelled_count


# Main task runner (call this from cron job or scheduler)
def run_all_escrow_tasks():
    """Run all escrow background tasks"""
    print("=" * 60)
    print("ESCROW BACKGROUND TASKS")
    print("=" * 60)
    
    auto_release_escrow()
    send_delivery_reminders()
    check_pending_payments()
    
    print("=" * 60)
    print("ALL TASKS COMPLETED")
    print("=" * 60)


if __name__ == '__main__':
    # For testing: python -m app.tasks.escrow_tasks
    from app import create_app
    app = create_app()
    with app.app_context():
        run_all_escrow_tasks()
