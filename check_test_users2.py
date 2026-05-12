"""Full test user list with all details."""
from app import create_app
from app.models.user import User
from app.models.order import Order

app = create_app()

TEST_PATTERNS = [
    'okereke', 'ngozi', 'stillwalker', 'tessy', 'innocent',
    'test', 'demo', 'sample', 'fake', 'dummy',
]

with app.app_context():
    all_users = {}
    for pattern in TEST_PATTERNS:
        users = User.query.filter(User.email.ilike(f'%{pattern}%')).all()
        for u in users:
            all_users[u.id] = (u, pattern)

    if not all_users:
        print("NO TEST USERS FOUND")
    else:
        print(f"FOUND {len(all_users)} TEST USER(S):")
        print()
        for uid, (user, pattern) in all_users.items():
            order_count = Order.query.filter(
                (Order.buyer_id == user.id) | (Order.vendor_id == user.id)
            ).count()
            storefront = user.storefront.store_name if user.storefront else "none"
            print(f"ID:{user.id} | {user.email} | {user.role} | verified:{user.is_verified} | orders:{order_count} | store:{storefront} | matched:'{pattern}'")
