import sys
import os

sys.path.insert(0, os.path.abspath('.'))
sys.stdout.reconfigure(encoding='utf-8', errors='replace')

from app import create_app
from app.extensions import db
from app.models.user import User
from app.models.order import Order, OrderItem, Cart, CartItem
from app.models.escrow import EscrowTransaction, LogisticsAssignment
from app.models.product import Product
from app.models.finance import Ledger, Invoice, Receipt
from app.models.crm import CustomerProfile
from app.models.partnerships import Referral, PartnerApplication
from app.models.communication import Notification, Message
from app.models.community import Review
from app.models.admin import Favorite

app = create_app()

with app.app_context():
    all_users = User.query.all()
    if not all_users:
        print("No users found. Database is already empty.")
    else:
        print(f"Wiping {len(all_users)} user(s) and all associated data...\n")
        deleted, failed = 0, 0

        for user in all_users:
            try:
                # All orders this user is involved in
                orders = Order.query.filter(
                    (Order.buyer_id == user.id) | (Order.vendor_id == user.id)
                ).all()

                for order in orders:
                    EscrowTransaction.query.filter_by(order_id=order.id).delete(synchronize_session=False)
                    LogisticsAssignment.query.filter_by(order_id=order.id).delete(synchronize_session=False)
                    OrderItem.query.filter_by(order_id=order.id).delete(synchronize_session=False)
                    Invoice.query.filter_by(order_id=order.id).delete(synchronize_session=False)
                    Receipt.query.filter_by(order_id=order.id).delete(synchronize_session=False)
                    Review.query.filter_by(order_id=order.id).delete(synchronize_session=False)
                    Notification.query.filter_by(order_id=order.id).delete(synchronize_session=False)
                    db.session.delete(order)

                # Cart
                cart = Cart.query.filter_by(user_id=user.id).first()
                if cart:
                    CartItem.query.filter_by(cart_id=cart.id).delete(synchronize_session=False)
                    db.session.delete(cart)

                # Storefront + products
                if user.storefront:
                    Product.query.filter_by(storefront_id=user.storefront.id).delete(synchronize_session=False)
                    db.session.delete(user.storefront)

                # Ledger
                Ledger.query.filter_by(vendor_id=user.id).delete(synchronize_session=False)

                # CRM
                CustomerProfile.query.filter_by(vendor_id=user.id).delete(synchronize_session=False)
                CustomerProfile.query.filter_by(buyer_id=user.id).delete(synchronize_session=False)

                # Referrals
                Referral.query.filter_by(referrer_id=user.id).delete(synchronize_session=False)
                Referral.query.filter_by(referred_id=user.id).delete(synchronize_session=False)

                # Partner application
                PartnerApplication.query.filter_by(user_id=user.id).delete(synchronize_session=False)

                # Notifications & messages
                Notification.query.filter_by(user_id=user.id).delete(synchronize_session=False)
                Message.query.filter_by(sender_id=user.id).delete(synchronize_session=False)
                Message.query.filter_by(receiver_id=user.id).delete(synchronize_session=False)

                # Reviews
                Review.query.filter_by(buyer_id=user.id).delete(synchronize_session=False)
                Review.query.filter_by(vendor_id=user.id).delete(synchronize_session=False)

                # Favorites
                Favorite.query.filter_by(user_id=user.id).delete(synchronize_session=False)

                # User
                db.session.delete(user)
                db.session.commit()
                deleted += 1

            except Exception as e:
                db.session.rollback()
                failed += 1

        print(f"\n{'='*40}")
        print(f"Done. Deleted: {deleted} | Failed: {failed}")
        print(f"{'='*40}")
