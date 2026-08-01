import logging
from datetime import datetime, timezone, timedelta
from app.extensions import db
from app.models.user import User, Storefront, UserRole
from app.utils.email import send_siiqo_email

logger = logging.getLogger(__name__)

def run_onboarding_email_sequence():
    """
    Sends automated onboarding emails:
    - Day 1: Welcome + Store Link (sent 1 day after storefront creation)
    - Day 3: Add your first product (sent 3 days after creation if 0 products)
    - Day 7: Share Pay Link on WhatsApp (sent 7 days after creation)
    """
    now = datetime.now(timezone.utc)
    storefronts = Storefront.query.all()
    count_sent = 0

    for sf in storefronts:
        if not sf.created_at:
            continue
        
        vendor = sf.vendor
        if not vendor or not vendor.email:
            continue

        days_since_created = (now - sf.created_at).days
        sent_dict = dict(sf.onboarding_emails_sent or {})

        store_url = f"https://siiqo.com/{sf.store_slug}"

        # Day 1 Email
        if days_since_created >= 1 and not sent_dict.get('day1'):
            try:
                send_siiqo_email(
                    to_email=vendor.email,
                    subject="Welcome to Siiqo! Your store link is live 🚀",
                    template_name="onboarding_day1",
                    first_name=vendor.first_name or "Vendor",
                    store_url=store_url
                )
                sent_dict['day1'] = now.isoformat()
                sf.onboarding_emails_sent = sent_dict
                db.session.commit()
                count_sent += 1
            except Exception as e:
                logger.error(f"[ONBOARDING SEQ ERR] Day 1 email failed for SF #{sf.id}: {e}")

        # Day 3 Email
        if days_since_created >= 3 and not sent_dict.get('day3'):
            has_products = len(sf.products or []) > 0
            if not has_products:
                try:
                    send_siiqo_email(
                        to_email=vendor.email,
                        subject="Add your first product to get orders 📦",
                        template_name="onboarding_day3",
                        first_name=vendor.first_name or "Vendor",
                        store_url=store_url
                    )
                    sent_dict['day3'] = now.isoformat()
                    sf.onboarding_emails_sent = sent_dict
                    db.session.commit()
                    count_sent += 1
                except Exception as e:
                    logger.error(f"[ONBOARDING SEQ ERR] Day 3 email failed for SF #{sf.id}: {e}")

        # Day 7 Email
        if days_since_created >= 7 and not sent_dict.get('day7'):
            try:
                send_siiqo_email(
                    to_email=vendor.email,
                    subject="Share your Pay Link on WhatsApp 💬",
                    template_name="onboarding_day7",
                    first_name=vendor.first_name or "Vendor",
                    store_url=store_url
                )
                sent_dict['day7'] = now.isoformat()
                sf.onboarding_emails_sent = sent_dict
                db.session.commit()
                count_sent += 1
            except Exception as e:
                logger.error(f"[ONBOARDING SEQ ERR] Day 7 email failed for SF #{sf.id}: {e}")

    logger.info(f"[ONBOARDING TASK] Completed sequence check. Sent {count_sent} emails.")
    return count_sent
