"""
Just lists test users — no deletion. Safe to run anytime.
"""
from app import create_app
from app.models.user import User
from app.models.order import Order

app = create_app()

TEST_PATTERNS = [
    'okereke', 'ngozi', 'stillwalker', 'tessy', 'innocent',
    'test', 'demo', 'sample', 'fake', 'dummy',
]

with app.app_context():
    print("\n" + "=" * 65)
    print("  SIIQO — Test User Check")
    print("=" * 65)

    found = {}
    for pattern in TEST_PATTERNS:
        users = User.query.filter(User.email.ilike(f'%{pattern}%')).all()
        if users:
            found[pattern] = users

    if not found:
        print("\n  ✅ No test users found. Database is clean!\n")
    else:
        total = sum(len(v) for v in found.values())
        # Deduplicate
        all_users = {u.id: u for v in found.values() for u in v}
        
        print(f"\n  Found {len(all_users)} user(s) matching test patterns:\n")
        print(f"  {'#':<4} {'Email':<40} {'Role':<10} {'Verified':<10} {'Orders':<8} {'Storefront'}")
        print(f"  {'-'*4} {'-'*40} {'-'*10} {'-'*10} {'-'*8} {'-'*20}")
        
        for i, (uid, user) in enumerate(all_users.items(), 1):
            order_count = Order.query.filter(
                (Order.buyer_id == user.id) | (Order.vendor_id == user.id)
            ).count()
            storefront = user.storefront.store_name if user.storefront else "—"
            verified = "✓" if user.is_verified else "✗"
            print(f"  {i:<4} {user.email:<40} {user.role:<10} {verified:<10} {order_count:<8} {storefront}")
        
        print(f"\n  Matched patterns: {', '.join(found.keys())}")
        print(f"\n  To delete these users, run:")
        print(f"  python cleanup_test_users.py\n")
    
    print("=" * 65 + "\n")
