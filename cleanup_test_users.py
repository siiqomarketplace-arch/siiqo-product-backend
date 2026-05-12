"""
Cleanup test users from the database.
Searches for test emails and deletes them along with all related data.
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

app = create_app()

# Test email patterns to search for
TEST_PATTERNS = [
    'okereke',
    'ngozi',
    'stillwalker',
    'tessy',
    'innocent',
    'test',
    'demo',
    'sample',
]

def find_test_users():
    """Find all users matching test patterns."""
    users = []
    for pattern in TEST_PATTERNS:
        found = User.query.filter(User.email.ilike(f'%{pattern}%')).all()
        users.extend(found)
    
    # Remove duplicates
    unique_users = {u.id: u for u in users}
    return list(unique_users.values())


def delete_user_cascade(user):
    """Delete a user and all related data."""
    print(f"\n🗑️  Deleting user: {user.email} (ID: {user.id}, Role: {user.role})")
    
    try:
        # 1. Delete orders (as buyer or vendor)
        orders_as_buyer = Order.query.filter_by(buyer_id=user.id).all()
        orders_as_vendor = Order.query.filter_by(vendor_id=user.id).all()
        all_orders = orders_as_buyer + orders_as_vendor
        
        for order in all_orders:
            # Delete escrow
            EscrowTransaction.query.filter_by(order_id=order.id).delete(synchronize_session=False)
            # Delete logistics
            LogisticsAssignment.query.filter_by(order_id=order.id).delete(synchronize_session=False)
            # Delete order items
            OrderItem.query.filter_by(order_id=order.id).delete(synchronize_session=False)
            # Delete invoice/receipt
            Invoice.query.filter_by(order_id=order.id).delete(synchronize_session=False)
            Receipt.query.filter_by(order_id=order.id).delete(synchronize_session=False)
            # Delete reviews
            Review.query.filter_by(order_id=order.id).delete(synchronize_session=False)
            # Delete order
            db.session.delete(order)
        
        print(f"   ✓ Deleted {len(all_orders)} orders")
        
        # 2. Delete cart
        cart = Cart.query.filter_by(user_id=user.id).first()
        if cart:
            CartItem.query.filter_by(cart_id=cart.id).delete(synchronize_session=False)
            db.session.delete(cart)
            print(f"   ✓ Deleted cart")
        
        # 3. Delete products (if vendor)
        if user.storefront:
            products = Product.query.filter_by(storefront_id=user.storefront.id).all()
            for product in products:
                db.session.delete(product)
            print(f"   ✓ Deleted {len(products)} products")
            
            # Delete storefront
            db.session.delete(user.storefront)
            print(f"   ✓ Deleted storefront: {user.storefront.store_name}")
        
        # 4. Delete ledger entries
        ledger_count = Ledger.query.filter_by(vendor_id=user.id).delete(synchronize_session=False)
        if ledger_count:
            print(f"   ✓ Deleted {ledger_count} ledger entries")
        
        # 5. Delete CRM profiles
        crm_as_vendor = CustomerProfile.query.filter_by(vendor_id=user.id).delete(synchronize_session=False)
        crm_as_buyer = CustomerProfile.query.filter_by(buyer_id=user.id).delete(synchronize_session=False)
        if crm_as_vendor or crm_as_buyer:
            print(f"   ✓ Deleted {crm_as_vendor + crm_as_buyer} CRM profiles")
        
        # 6. Delete referrals
        ref_made = Referral.query.filter_by(referrer_id=user.id).delete(synchronize_session=False)
        ref_received = Referral.query.filter_by(referred_id=user.id).delete(synchronize_session=False)
        if ref_made or ref_received:
            print(f"   ✓ Deleted {ref_made + ref_received} referrals")
        
        # 7. Delete partner application
        partner_app = PartnerApplication.query.filter_by(user_id=user.id).first()
        if partner_app:
            db.session.delete(partner_app)
            print(f"   ✓ Deleted partner application")
        
        # 8. Delete notifications
        notif_count = Notification.query.filter_by(user_id=user.id).delete(synchronize_session=False)
        if notif_count:
            print(f"   ✓ Deleted {notif_count} notifications")
        
        # 9. Delete messages
        msg_sent = Message.query.filter_by(sender_id=user.id).delete(synchronize_session=False)
        msg_received = Message.query.filter_by(receiver_id=user.id).delete(synchronize_session=False)
        if msg_sent or msg_received:
            print(f"   ✓ Deleted {msg_sent + msg_received} messages")
        
        # 10. Delete reviews
        review_count = Review.query.filter_by(buyer_id=user.id).delete(synchronize_session=False)
        if review_count:
            print(f"   ✓ Deleted {review_count} reviews")
        
        # 11. Finally, delete the user
        db.session.delete(user)
        db.session.commit()
        
        print(f"   ✅ User {user.email} deleted successfully")
        return True
        
    except Exception as e:
        db.session.rollback()
        print(f"   ❌ Error deleting {user.email}: {str(e)}")
        return False


def main():
    with app.app_context():
        print("=" * 60)
        print("🔍 Searching for test users...")
        print("=" * 60)
        
        test_users = find_test_users()
        
        if not test_users:
            print("\n✅ No test users found. Database is clean!")
            return
        
        print(f"\n📋 Found {len(test_users)} test user(s):\n")
        for i, user in enumerate(test_users, 1):
            storefront_info = f" | Storefront: {user.storefront.store_name}" if user.storefront else ""
            print(f"   {i}. {user.email} (ID: {user.id}, Role: {user.role}){storefront_info}")
        
        print("\n" + "=" * 60)
        response = input("\n⚠️  Delete ALL these users? (yes/no): ").strip().lower()
        
        if response != 'yes':
            print("\n❌ Cancelled. No users were deleted.")
            return
        
        print("\n🗑️  Starting deletion...\n")
        
        success_count = 0
        fail_count = 0
        
        for user in test_users:
            if delete_user_cascade(user):
                success_count += 1
            else:
                fail_count += 1
        
        print("\n" + "=" * 60)
        print(f"✅ Deletion complete!")
        print(f"   • Successfully deleted: {success_count}")
        print(f"   • Failed: {fail_count}")
        print("=" * 60)


if __name__ == '__main__':
    main()
