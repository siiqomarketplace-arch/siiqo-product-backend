"""Quick verification: imports, app creation, route registration."""
import sys
sys.stdout.reconfigure(encoding='utf-8', errors='replace')
from app import create_app

app = create_app()

with app.app_context():
    rules = sorted(str(r) for r in app.url_map.iter_rules())
    api_rules = [r for r in rules if r.startswith('/api')]
    print(f"\n✅  App created OK — {len(api_rules)} API routes registered\n")

    # Check critical routes exist
    required = [
        '/api/auth/register',
        '/api/auth/login',
        '/api/auth/verify-email',
        '/api/auth/refresh',
        '/api/cart/checkout',
        '/api/escrow/webhook',
        '/api/escrow/release',
        '/api/admin/categories',
        '/api/admin/escrow/release',
        '/api/admin/escrow/verify',
        '/api/admin/partners',
        '/api/admin/users',
        '/api/logistics/staff',
        '/api/chat/threads',
        '/api/chat/notifications/read-all',
        '/api/buyer-orders/confirm-received',
        '/api/reviews',
    ]

    missing = []
    for route in required:
        if not any(route in r for r in api_rules):
            missing.append(route)

    if missing:
        print("❌  Missing routes:")
        for r in missing:
            print(f"   {r}")
        sys.exit(1)
    else:
        print("✅  All critical routes present\n")

    # Print full API route list
    for r in api_rules:
        print(f"   {r}")
