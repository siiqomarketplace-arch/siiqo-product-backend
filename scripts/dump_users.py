from app import create_app
from app.models.user import User

app = create_app('development')
with app.app_context():
    users = User.query.all()
    print("USERS:")
    for u in users:
        store = getattr(u, 'storefront', None)
        print(f"ID: {u.id}, Email: {u.email}, First Name: {u.first_name}, Role: {u.role}")
        if store:
            print(f"   Storefront: {store.store_name}, Slug: {store.store_slug}, Published: {store.is_published}")
        else:
            print("   No Storefront")
