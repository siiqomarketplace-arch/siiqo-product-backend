import logging
from datetime import datetime, timezone, timedelta
from app.extensions import db
from app.models.user import User, Storefront, UserRole
from app.utils.email import send_siiqo_email
from app.utils.telegram import send_telegram_message

logger = logging.getLogger(__name__)

def run_onboarding_email_sequence():
    """
    Sends automated onboarding notifications (Email + Telegram Bot):
    - Day 1: Welcome + Store Link (sent 1 day after storefront creation)
    - Day 3: Add your first product (sent 3 days after creation if 0 products)
    - Day 7: Share store on Telegram/Channels (sent 7 days after creation)
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
        vendor_name = vendor.first_name or "Vendor"
        tg_id = vendor.telegram_id

        # Day 1 Notification
        if days_since_created >= 1 and not sent_dict.get('day1'):
            try:
                send_siiqo_email(
                    to_email=vendor.email,
                    subject="Welcome to Siiqo! Your store link is live 🚀",
                    template_name="onboarding_day1",
                    first_name=vendor_name,
                    store_url=store_url
                )
                if tg_id:
                    tg_msg = (
                        f"🎉 <b>Welcome to Siiqo, {vendor_name}!</b>\n\n"
                        f"Your online storefront is officially live at <a href='{store_url}'>{store_url}</a>.\n\n"
                        f"⚡ <b>Next Step:</b> Add your first product so buyers in your category can browse and order securely with escrow protection!"
                    )
                    send_telegram_message(tg_id, tg_msg)

                sent_dict['day1'] = now.isoformat()
                sf.onboarding_emails_sent = sent_dict
                db.session.commit()
                count_sent += 1
            except Exception as e:
                logger.error(f"[ONBOARDING SEQ ERR] Day 1 notification failed for SF #{sf.id}: {e}")

        # Day 3 Notification (Nudge if 0 products)
        if days_since_created >= 3 and not sent_dict.get('day3'):
            has_products = len(sf.products or []) > 0
            if not has_products:
                try:
                    send_siiqo_email(
                        to_email=vendor.email,
                        subject="Add your first product to get orders 📦",
                        template_name="onboarding_day3",
                        first_name=vendor_name,
                        store_url=store_url
                    )
                    if tg_id:
                        tg_msg = (
                            f"📦 <b>Hey {vendor_name}!</b>\n\n"
                            f"Shoppers are browsing Siiqo, but your store doesn't have any items listed yet! "
                            f"Take 60 seconds to upload 1 photo and price:\n"
                            f"👉 <a href='https://siiqo.com/vendor/products'>Add Your First Product Now</a>"
                        )
                        send_telegram_message(tg_id, tg_msg)

                    sent_dict['day3'] = now.isoformat()
                    sf.onboarding_emails_sent = sent_dict
                    db.session.commit()
                    count_sent += 1
                except Exception as e:
                    logger.error(f"[ONBOARDING SEQ ERR] Day 3 notification failed for SF #{sf.id}: {e}")

        # Day 7 Notification (Growth & Distribution Nudge)
        if days_since_created >= 7 and not sent_dict.get('day7'):
            try:
                send_siiqo_email(
                    to_email=vendor.email,
                    subject="Share your Pay Link and Storefront 💬",
                    template_name="onboarding_day7",
                    first_name=vendor_name,
                    store_url=store_url
                )
                if tg_id:
                    tg_msg = (
                        f"🚀 <b>Keep the momentum going, {vendor_name}!</b>\n\n"
                        f"Share your store link with your audience across Telegram and social channels:\n"
                        f"👉 <a href='{store_url}'>{store_url}</a>\n\n"
                        f"💡 <i>Vendors who actively share their store link get orders within their first week!</i>"
                    )
                    send_telegram_message(tg_id, tg_msg)

                sent_dict['day7'] = now.isoformat()
                sf.onboarding_emails_sent = sent_dict
                db.session.commit()
                count_sent += 1
            except Exception as e:
                logger.error(f"[ONBOARDING SEQ ERR] Day 7 notification failed for SF #{sf.id}: {e}")

    logger.info(f"[ONBOARDING TASK] Completed sequence check. Processed notifications for {count_sent} storefronts.")
    return count_sent

