"""
fee_sweep_tasks.py — Siiqo Platform Fee Auto-Sweep

Runs on a scheduled interval (every 6 hours by default).
Checks how much platform fee (5% standard / 3% verified) has accumulated in
Siiqo's Daya collection balance and, when it exceeds the configured
threshold (default: ₦20,000), automatically transfers those funds to
Siiqo's corporate NGN bank account.

Environment variables used:
  SIIQO_CORP_BANK_CODE       — Siiqo's corporate bank code (e.g. "044" for Access Bank)
  SIIQO_CORP_ACCOUNT_NUMBER  — Siiqo's corporate bank account number
  SIIQO_CORP_ACCOUNT_NAME    — Siiqo's corporate account holder name (optional, speeds resolution)
  SIIQO_FEE_SWEEP_THRESHOLD  — NGN threshold before auto-sweep fires (default: 20000)
"""

import logging
import os
import uuid
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Config helpers
# ---------------------------------------------------------------------------

SWEEP_THRESHOLD_NGN = float(os.environ.get("SIIQO_FEE_SWEEP_THRESHOLD", "20000"))
CORP_BANK_CODE = os.environ.get("SIIQO_CORP_BANK_CODE", "")
CORP_ACCOUNT_NUMBER = os.environ.get("SIIQO_CORP_ACCOUNT_NUMBER", "")
CORP_ACCOUNT_NAME = os.environ.get("SIIQO_CORP_ACCOUNT_NAME", "Siiqo Marketplace Ltd")


def _utcnow():
    return datetime.now(timezone.utc)


# ---------------------------------------------------------------------------
# Core sweep function
# ---------------------------------------------------------------------------

def run_platform_fee_sweep():
    """
    Main entry point called by the scheduler.

    Steps:
    1. Fetch Siiqo's live Daya merchant balance.
    2. Calculate the total accumulated Siiqo platform fees (from RELEASED escrows
       paid via Daya, not yet swept) from the database.
    3. If accumulated fees >= SWEEP_THRESHOLD_NGN:
       a. Convert target NGN to USD using live Daya rate.
       b. Ensure enough funds are in Daya withdrawal_balance (move from collection if needed).
       c. Trigger a Daya NGN bank transfer to Siiqo's corporate bank account.
       d. Record the sweep in the SiiqoFeeSweep ledger table.
    """
    from app.extensions import db
    from app.services import daya_service
    from app.models.fee_sweep import SiiqoFeeSweep
    from sqlalchemy import func

    logger.info("[FEE SWEEP] Starting platform fee auto-sweep check …")

    # Guard: corporate bank account must be configured
    if not CORP_BANK_CODE or not CORP_ACCOUNT_NUMBER:
        logger.warning(
            "[FEE SWEEP] SIIQO_CORP_BANK_CODE or SIIQO_CORP_ACCOUNT_NUMBER not set — "
            "auto-sweep is disabled until these env vars are configured."
        )
        return

    try:
        # ── 1. Total fees earned from RELEASED Daya escrows (all time) ────────
        from app.models.escrow import EscrowTransaction, EscrowStatus
        total_released_fees_ngn = db.session.query(
            func.sum(EscrowTransaction.fee_amount)
        ).filter(
            EscrowTransaction.status == EscrowStatus.RELEASED,
        ).scalar() or 0.0
        total_released_fees_ngn = float(total_released_fees_ngn)

        # ── 2. Total already swept to Siiqo bank ──────────────────────────────
        total_swept_ngn = db.session.query(
            func.sum(SiiqoFeeSweep.amount_ngn)
        ).filter(
            SiiqoFeeSweep.status == "SUCCESS"
        ).scalar() or 0.0
        total_swept_ngn = float(total_swept_ngn)

        # ── 3. Net accumulated (not yet swept) ────────────────────────────────
        accumulated_ngn = max(total_released_fees_ngn - total_swept_ngn, 0.0)

        logger.info(
            "[FEE SWEEP] Total released fees: ₦%.2f | Already swept: ₦%.2f | "
            "Accumulated: ₦%.2f | Threshold: ₦%.2f",
            total_released_fees_ngn, total_swept_ngn, accumulated_ngn, SWEEP_THRESHOLD_NGN
        )

        if accumulated_ngn < SWEEP_THRESHOLD_NGN:
            logger.info(
                "[FEE SWEEP] Below threshold — no sweep needed (₦%.2f < ₦%.2f)",
                accumulated_ngn, SWEEP_THRESHOLD_NGN
            )
            return

        # ── 4. Get live FX rate ───────────────────────────────────────────────
        try:
            rate_info = daya_service.get_rate(asset="USDT", side="SELL")
            fx_rate = float(rate_info.get("rate", 1500.0))
        except Exception:
            fx_rate = 1500.0

        # ── 5. Compute USD equivalent + buffer ───────────────────────────────
        sweep_ngn = round(accumulated_ngn, 2)
        required_usd = round((sweep_ngn / fx_rate) * 1.02, 4)   # 2% buffer for rate drift

        # ── 6. Check / move Daya withdrawal balance ───────────────────────────
        try:
            balance = daya_service.get_merchant_balance()
            bal_data = balance.get("data", {})
            collection_usd = float(bal_data.get("collection_balance_usd", 0))
            withdrawal_usd = float(bal_data.get("withdrawal_balance_usd", 0))

            logger.info(
                "[FEE SWEEP] Daya balances — collection: $%.4f  withdrawal: $%.4f  "
                "required: $%.4f  rate: %.2f",
                collection_usd, withdrawal_usd, required_usd, fx_rate
            )

            if withdrawal_usd < required_usd:
                shortfall = required_usd - withdrawal_usd
                to_move = min(round(shortfall + 0.20, 4), collection_usd)
                if to_move > 0:
                    idem = f"fee-sweep-bal-{uuid.uuid4().hex[:10]}"
                    daya_service.transfer_collection_to_withdrawal(
                        amount_usd=to_move,
                        idempotency_key=idem,
                    )
                    logger.info(
                        "[FEE SWEEP] Moved $%.4f from collection to withdrawal.", to_move
                    )
        except Exception as bal_exc:
            logger.warning(
                "[FEE SWEEP] Balance check/move failed: %s — attempting sweep anyway.", bal_exc
            )

        # ── 7. Create the sweep record BEFORE transfer (track idempotency) ────
        sweep_ref = f"SIIQO-SWEEP-{uuid.uuid4().hex[:10].upper()}"
        sweep_record = SiiqoFeeSweep(
            reference=sweep_ref,
            amount_ngn=sweep_ngn,
            amount_usd=required_usd,
            fx_rate=fx_rate,
            bank_code=CORP_BANK_CODE,
            account_number=CORP_ACCOUNT_NUMBER,
            account_name=CORP_ACCOUNT_NAME,
            status="PENDING",
        )
        db.session.add(sweep_record)
        db.session.commit()

        # ── 8. Trigger Daya NGN bank transfer to Siiqo corp account ──────────
        try:
            result = daya_service.transfer_ngn_to_vendor(
                amount_ngn=sweep_ngn,
                bank_code=CORP_BANK_CODE,
                account_number=CORP_ACCOUNT_NUMBER,
                account_name=CORP_ACCOUNT_NAME,
                reference=sweep_ref,
            )

            if result.get("success"):
                sweep_record.status = "SUCCESS"
                sweep_record.daya_transfer_id = result.get("transfer_id")
                sweep_record.completed_at = _utcnow()
                logger.info(
                    "[FEE SWEEP] ✅ SUCCESS — Swept ₦%.2f to Siiqo corporate account. "
                    "Daya transfer ID: %s  ref: %s",
                    sweep_ngn, result.get("transfer_id"), sweep_ref
                )
            else:
                sweep_record.status = "FAILED"
                sweep_record.error_message = result.get("error_message", "Unknown error")
                logger.error(
                    "[FEE SWEEP] ❌ Transfer FAILED — ref: %s  error: %s",
                    sweep_ref, sweep_record.error_message
                )

        except Exception as transfer_exc:
            sweep_record.status = "FAILED"
            sweep_record.error_message = str(transfer_exc)
            logger.error(
                "[FEE SWEEP] ❌ Transfer exception — ref: %s  error: %s",
                sweep_ref, transfer_exc
            )

        db.session.commit()

    except Exception as exc:
        logger.error("[FEE SWEEP] Unexpected error: %s", exc, exc_info=True)
        try:
            db.session.rollback()
        except Exception:
            pass
