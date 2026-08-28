"""
pro_badge_expiry_tasks.py — Siiqo Pro Badge Expiry & Auto-Revocation Task

Runs on a scheduled interval (every 6 hours).
Checks for vendors whose Pro Verified badge accreditation (pro_verified_expires_at)
has passed, sets is_pro_verified = False, updates verification_status,
notifies the vendor to renew, and recalculates their trust score.
"""
import logging
from datetime import datetime, timezone

from app.extensions import db
from app.models.user import Storefront, User
from app.models.communication import Notification
from app.services.trust import recalculate_vendor_trust

logger = logging.getLogger(__name__)


def _utcnow():
    return datetime.now(timezone.utc)


def run_pro_badge_expiry_check():
    """
    Scans all storefronts where is_pro_verified == True and
    pro_verified_expires_at < utcnow().
    Revokes the Pro Verified badge and sends renewal notification.
    """
    now = _utcnow()
    try:
        expired_storefronts = Storefront.query.filter(
            Storefront.is_pro_verified == True,
            Storefront.pro_verified_expires_at.isnot(None),
            Storefront.pro_verified_expires_at < now,
        ).all()

        if not expired_storefronts:
            logger.info("[PRO BADGE EXPIRY] No expired badges found.")
            return

        logger.info(f"[PRO BADGE EXPIRY] Found {len(expired_storefronts)} expired Pro Verified badges.")

        for sf in expired_storefronts:
            sf.is_pro_verified = False
            if sf.verification_status == 'VERIFIED':
                sf.verification_status = 'EXPIRED'

            # Notify vendor
            db.session.add(Notification(
                user_id=sf.vendor_id,
                title="Pro Verified Shield Expired 🛡️",
                message=(
                    f"Your Pro Verified accreditation for '{sf.store_name}' has expired. "
                    "Renew your accreditation to maintain 3% transaction fees, unlimited listings, "
                    "and same-day settlement."
                ),
                type="ACCOUNT",
            ))

            # Recalculate trust score
            try:
                recalculate_vendor_trust(sf.vendor_id, reason="Pro Verified Badge Expired")
            except Exception as trust_err:
                logger.warning(f"[PRO BADGE EXPIRY] Trust recalc skipped for vendor {sf.vendor_id}: {trust_err}")

        db.session.commit()
        logger.info(f"[PRO BADGE EXPIRY] Successfully revoked {len(expired_storefronts)} expired badges.")

    except Exception as e:
        logger.error(f"[PRO BADGE EXPIRY] Critical error checking badge expiries: {e}")
        db.session.rollback()
