import sys
import os

# Ensure the correct path
sys.path.insert(0, os.path.abspath('.'))

sys.stdout.reconfigure(encoding='utf-8', errors='replace')
try:
    from app import create_app
    from app.models.user import User, Storefront
    from app.models.order import Order
    from app.models.product import Product

    app = create_app()
    with app.app_context():
        total_users = User.query.count()
        verified_users = User.query.filter_by(is_verified=True).count()
        total_storefronts = Storefront.query.count()
        total_orders = Order.query.count()
        total_products = Product.query.count()
        
        print("--- Siiqo Database Statistics ---")
        print(f"Total Users: {total_users}")
        print(f"Verified Users: {verified_users}")
        print(f"Users with Storefronts: {total_storefronts}")
        print(f"Total Products: {total_products}")
        print(f"Total Orders: {total_orders}")
        print("---------------------------------")
except Exception as e:
    print(f"Error: {e}")
