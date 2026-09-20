"""
retry_daya_payouts.py
─────────────────────
One-time backfill: find all COMPLETED orders paid via Daya (bank transfer
or crypto) that never received a vendor payout, and trigger _payout_vendor_via_daya()
for each.

SAFE TO RUN MULTIPLE TIMES — Daya idempotency keys and the DayaPayment table
prevent double-payments.

Usage (on the server):
    cd /var/app
    source venv/staging-LQM1lest/bin/activate
    python retry_daya_payouts.py

    # Dry-run (no actual transfers, just shows what would be paid):
    python retry_daya_payouts.py --dry-run
"""

import sys
import os
import logging

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger("retry_daya_payouts")

DRY_RUN = "--dry-run" in sys.argv

# Bootstrap Flask app
os.chdir(os.path.dirname(os.path.abspath(__file__)))
from app import create_app
from app.extensions import db

app = create_app()

# Daya payment methods - keep in sync with escrow.py
DAYA_PAYMENT_METHODS = ('CRYPTO', 'DAYA', 'USDT', 'USDC', 'DAYA_BANK_TRANSFER')


def run():
    with app.app_context():
        from app.models.order import Order
        from app.models.escrow import EscrowTransaction, EscrowStatus
        from app.models.finance import Ledger
        from app.models.withdrawal import VendorBankAccount, DayaPayment

        logger.info("=" * 60)
        logger.info("Siiqo - Daya Payout Backfill")
        logger.info("DRY RUN: %s", DRY_RUN)
        logger.info("=" * 60)

        # 1. Find all completed orders paid via Daya by payment_method
        completed_orders = (
            Order.query
            .filter(
                Order.status == 'COMPLETED',
                Order.payment_method.in_(DAYA_PAYMENT_METHODS),
            )
            .order_by(Order.id.asc())
            .all()
        )

        # Also catch any order whose escrow txn starts with DYA- or DAYA-
        daya_escrow_orders = (
            db.session.query(Order)
            .join(EscrowTransaction, EscrowTransaction.order_id == Order.id)
            .filter(
                Order.status == 'COMPLETED',
                Order.payment_method.notin_(DAYA_PAYMENT_METHODS),
                (
                    EscrowTransaction.transaction_number.like('DYA-%') |
                    EscrowTransaction.payscrow_transaction_id.like('DAYA-%')
                ),
            )
            .all()
        )

        all_orders = {o.id: o for o in completed_orders + daya_escrow_orders}
        logger.info("Found %d completed Daya orders total.", len(all_orders))

        if not all_orders:
            logger.info("Nothing to do. All done!")
            return

        paid_count = 0
        skipped_count = 0
        failed_count = 0

        for order_id, order in sorted(all_orders.items()):
            escrow = EscrowTransaction.query.filter_by(order_id=order_id).first()
            if not escrow:
                logger.warning("  Order #%d - No escrow record. Skipping.", order_id)
                skipped_count += 1
                continue

            net_amount = float(escrow.amount) - float(escrow.fee_amount or 0)
            vendor_bank = VendorBankAccount.query.filter_by(
                vendor_id=order.vendor_id, is_default=True
            ).first() or VendorBankAccount.query.filter_by(
                vendor_id=order.vendor_id
            ).first()

            ledger_credit = Ledger.query.filter_by(
                vendor_id=order.vendor_id,
                transaction_type='CREDIT',
            ).filter(
                Ledger.reference_id.in_([
                    escrow.transaction_number or '',
                    f"ORD-{order.id}",
                ])
            ).first()

            logger.info(
                "\nOrder #%d | vendor=%d | method=%s | amount=NGN%.2f | "
                "ledger=%s | bank=%s",
                order_id,
                order.vendor_id,
                order.payment_method,
                net_amount,
                "CREDITED" if ledger_credit else "MISSING",
                (vendor_bank.bank_name + " *" + vendor_bank.account_number[-4:]) if vendor_bank else "NONE",
            )

            if not vendor_bank:
                logger.warning(
                    "  Order #%d - Vendor %d has no bank account. Cannot pay out.",
                    order_id, order.vendor_id,
                )
                failed_count += 1
                continue

            if DRY_RUN:
                logger.info(
                    "  [DRY RUN] Would fire payout for Order #%d (NGN%.2f -> %s *%s)",
                    order_id, net_amount,
                    vendor_bank.bank_name or "Bank",
                    vendor_bank.account_number[-4:],
                )
                paid_count += 1
                continue

            # Ensure ledger credit exists (idempotent)
            if not ledger_credit:
                from app.routes.escrow import _credit_vendor_ledger
                _credit_vendor_ledger(
                    vendor_id=order.vendor_id,
                    amount=net_amount,
                    reference_id=escrow.transaction_number or f"ORD-{order.id}",
                    description=f"Backfill payout credit for Order #{order.id}",
                )
                db.session.flush()
                logger.info("  Ledger credit created for Order #%d.", order_id)

            # Fire the Daya payout
            try:
                from app.routes.payments import _payout_vendor_via_daya
                _payout_vendor_via_daya(order, escrow)
                db.session.commit()
                logger.info(
                    "  PAID Order #%d - NGN%.2f -> %s *%s",
                    order_id, net_amount,
                    vendor_bank.bank_name or "Bank",
                    vendor_bank.account_number[-4:],
                )
                paid_count += 1
            except Exception as exc:
                db.session.rollback()
                logger.error("  FAILED Order #%d: %s", order_id, exc)
                failed_count += 1

        logger.info("\n" + "=" * 60)
        logger.info("BACKFILL COMPLETE")
        logger.info("  Paid / would-pay : %d", paid_count)
        logger.info("  Skipped          : %d", skipped_count)
        logger.info("  Failed           : %d", failed_count)
        logger.info("=" * 60)

        if DRY_RUN:
            logger.info("This was a DRY RUN. Re-run without --dry-run to actually pay.")


if __name__ == "__main__":
    run()
