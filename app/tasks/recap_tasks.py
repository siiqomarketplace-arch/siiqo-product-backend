import logging
from datetime import datetime, timezone, timedelta
from app.extensions import db
from app.models.user import User, Storefront
from app.models.order import Order
from app.models.product import Product
from app.utils.email import send_siiqo_email

logger = logging.getLogger(__name__)

def run_monday_recap_task():
    """
    Weekly summary sent every Monday 8am WAT (7am UTC):
    - store visits
    - product views
    - orders
    - revenue
    """
    now = datetime.now(timezone.utc)
    seven_days_ago = now - timedelta(days=7)
    date_str = now.strftime("%b %d, %Y")

    storefronts = Storefront.query.filter_by(is_verified=True, is_published=True).all()
    count_sent = 0

    for sf in storefronts:
        vendor = sf.vendor
        if not vendor or not vendor.email:
            continue

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

        try:
            send_siiqo_email(
                to_email=vendor.email,
                subject="Your Weekly Siiqo Store Recap 📊",
                template_name="monday_recap",
                first_name=vendor.first_name or "Vendor",
                date_str=date_str,
                store_visits=store_visits,
                product_views=product_views,
                orders_count=orders_count,
                revenue=revenue_str,
            )
            count_sent += 1
        except Exception as e:
            logger.error(f"[MONDAY RECAP ERR] Failed for SF #{sf.id}: {e}")

    logger.info(f"[MONDAY RECAP TASK] Sent weekly recap to {count_sent} vendors.")
    return count_sent
