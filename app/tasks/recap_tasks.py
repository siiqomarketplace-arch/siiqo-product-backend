import logging
from datetime import datetime, timezone, timedelta
from app.extensions import db
from app.models.user import User, Storefront
from app.models.order import Order
from app.models.product import Product
from app.utils.email import send_siiqo_email
from app.utils.telegram import send_telegram_message

logger = logging.getLogger(__name__)

def run_monday_recap_task():
    """
    Weekly summary sent every Monday 8am WAT (7am UTC):
    - store visits
    - product views
    - orders
    - revenue
    Sent via Email and direct Telegram Bot push.
    """
    now = datetime.now(timezone.utc)
    seven_days_ago = now - timedelta(days=7)
    five_days_ago = now - timedelta(days=5)
    date_str = now.strftime("%b %d, %Y")

    storefronts = Storefront.query.filter_by(is_verified=True, is_published=True).all()
    count_sent = 0

    for sf in storefronts:
        vendor = sf.vendor
        if not vendor or not vendor.email:
            continue

        # Deduplication guard: skip if weekly recap was already sent for this storefront within 5 days
        sent_dict = dict(sf.onboarding_emails_sent or {})
        last_recap_raw = sent_dict.get('weekly_recap_sent_at')
        if last_recap_raw:
            try:
                last_recap_dt = datetime.fromisoformat(last_recap_raw.replace('Z', '+00:00'))
                if last_recap_dt >= five_days_ago:
                    logger.info(f"[MONDAY RECAP] Storefront #{sf.id} already received recap at {last_recap_raw}. Skipping duplicate.")
                    continue
            except Exception:
                pass

        # Calculate orders & revenue in past 7 days
        recent_orders = Order.query.filter(
            Order.vendor_id == vendor.id,
            Order.created_at >= seven_days_ago,
            Order.status.in_(['COMPLETED', 'PAID', 'DELIVERED', 'SHIPPED', 'IN_ESCROW'])
        ).all()

        orders_count = len(recent_orders)
        revenue_total = sum(float(o.total_amount) for o in recent_orders)
        revenue_str = f"₦{revenue_total:,.2f}"

        products = Product.query.filter_by(storefront_id=sf.id, is_active=True).all()
        product_views = sum(p.view_count or 0 for p in products)
        store_visits = sf.view_count or 0
        vendor_name = vendor.first_name or "Vendor"
        tg_id = vendor.telegram_id

        try:
            send_siiqo_email(
                to_email=vendor.email,
                subject="Your Weekly Siiqo Store Recap 📊",
                template_name="monday_recap",
                first_name=vendor_name,
                date_str=date_str,
                store_visits=store_visits,
                product_views=product_views,
                orders_count=orders_count,
                revenue=revenue_str,
            )

            # Push Telegram Recap if linked
            if tg_id:
                tg_msg = (
                    f"📊 <b>Weekly Store Performance — {date_str}</b>\n\n"
                    f"Hey {vendor_name}! Here is your 7-day business summary on Siiqo:\n\n"
                    f"👁️ <b>Store Visits:</b> {store_visits}\n"
                    f"📦 <b>Product Views:</b> {product_views}\n"
                    f"🛒 <b>Orders:</b> {orders_count}\n"
                    f"💰 <b>Revenue:</b> {revenue_str}\n\n"
                    f"💡 <i>Tip: Updating your catalog or sharing links early in the week drives stronger weekend sales.</i>\n"
                    f"👉 <a href='https://siiqo.com/vendor/dashboard'>Open Siiqo Dashboard</a>"
                )
                send_telegram_message(tg_id, tg_msg)

            count_sent += 1

            # Update timestamp in JSONB column to prevent duplicate sending across worker processes
            sent_dict['weekly_recap_sent_at'] = now.isoformat()
            sf.onboarding_emails_sent = sent_dict
            from sqlalchemy.orm.attributes import flag_modified
            flag_modified(sf, 'onboarding_emails_sent')
            db.session.commit()
        except Exception as e:
            db.session.rollback()
            logger.error(f"[MONDAY RECAP ERR] Failed for SF #{sf.id}: {e}")

    logger.info(f"[MONDAY RECAP TASK] Sent weekly recap to {count_sent} vendors.")
    return count_sent

