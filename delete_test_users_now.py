"""
Auto-deletes test users — no prompt, no confirmation needed.
"""
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

TEST_PATTERNS = [
    'okereke', 'ngozi', 'stillwalker', 'tessy', 'innocent',
    'test', 'demo', 'sample', 'fake', 'dummy',
]

with app.app_context():
    # Collect unique users
    all_users = {}
    for pattern in TEST_PATTERNS:
        for u in User.query.filter(User.email.ilike(f'%{pattern}%')).all():
            all_users[u.id] = u

    if not all_users:
        print("No test users found. Nothing to delete.")
    else:
        print(f"Deleting {len(all_users)} test user(s)...\n")
        deleted, failed = 0, 0

        for uid, user in all_users.items():
            try:
                print(f"  Deleting: {user.email} (ID:{user.id}, Role:{user.role})")

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
                print(f"  ✅ Deleted: {user.email}")
                deleted += 1

            except Exception as e:
                db.session.rollback()
                print(f"  ❌ Failed: {user.email} — {str(e)}")
                failed += 1

        print(f"\n{'='*40}")
        print(f"Done. Deleted: {deleted} | Failed: {failed}")
        print(f"{'='*40}")
