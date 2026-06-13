"""
recalculate_trust_scores.py — Scheduled task to recalculate trust scores for all vendors.
"""
import logging
logger = logging.getLogger(__name__)

from app.extensions import db
from app.models.user import User
from app.services.trust import recalculate_vendor_trust

def run_recalculate_all_trust_scores():
    logger.info("Starting scheduled recalculation of all vendor trust scores...")
    try:
        vendors = User.query.filter_by(role='VENDOR').all()
        logger.info(f"Found {len(vendors)} vendors to process.")
        
        success_count = 0
        error_count = 0
        
        for vendor in vendors:
            try:
                profile = recalculate_vendor_trust(vendor.id, reason="Scheduled Hourly Recalculation")
                if profile:
                    success_count += 1
                else:
                    error_count += 1
            except Exception as e:
                logger.error(f"Failed to recalculate trust for vendor {vendor.id}: {e}")
                error_count += 1
                
        logger.info(f"Trust recalculation finished. Processed {len(vendors)} vendors. Success: {success_count}, Failures: {error_count}")
        return success_count
    except Exception as e:
        logger.critical(f"Critical failure running trust recalculation cron task: {e}")
        return 0

if __name__ == '__main__':
    from app import create_app
    app = create_app()
    with app.app_context():
        # Setup basic logging to stdout when run directly
        logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
        run_recalculate_all_trust_scores()
