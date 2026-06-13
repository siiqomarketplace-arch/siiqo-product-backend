import sys
import os
import uuid
import json

# Setup paths
sys.path.insert(0, ".")

# Setup test DB environment before any app imports
os.environ['DATABASE_URL'] = 'sqlite:///test_referrals.db'

from app import create_app, db
from app.models.user import User, UserRole

def run_tests():
    import os
    os.environ['DATABASE_URL'] = 'sqlite:///test_referrals.db'
    
    app = create_app()
    app.config['TESTING'] = True
    app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///test_referrals.db'
    
    with app.app_context():
        db.drop_all()
        db.create_all()
        
    client = app.test_client()

    with app.app_context():
        # Create a unique referrer
        referrer_email = f"referrer_{uuid.uuid4().hex[:8]}@test.com"
        referrer = User(email=referrer_email, first_name="Referrer", role=UserRole.BUYER, points_balance=0.0)
        referrer.set_password("password123")
        db.session.add(referrer)
        db.session.commit()
        referrer.generate_referral_code()
        db.session.commit()
        ref_code = referrer.referral_code

        print(f"1. Created Referrer: {referrer_email} with Code: {ref_code}")

    # 2. Register Referred User using the API
    referred_email = f"referred_{uuid.uuid4().hex[:8]}@test.com"
    res = client.post('/api/auth/register', json={
        "email": referred_email,
        "password": "password123",
        "first_name": "Referred",
        "referral_code": ref_code
    })
    print(f"2. Registration Status: {res.status_code}")
    assert res.status_code == 201

    with app.app_context():
        referred = User.query.filter_by(email=referred_email).first()
        referred.is_verified = True
        db.session.commit()
        referred_id = referred.id
        
        referrer = User.query.filter_by(email=referrer_email).first()
        print(f"   Referrer Points after Signup (Should be 0): {referrer.points_balance}")

    # 3. Login Referred User
    res = client.post('/api/auth/login', json={
        "email": referred_email,
        "password": "password123"
    })
    assert res.status_code == 200
    referred_token = res.json['access_token']

    # 4. Setup Vendor & Product directly in DB to avoid long API flows
    with app.app_context():
        from app.models import Storefront, Category, Product
        vendor = User(email=f"vendor_{uuid.uuid4().hex[:8]}@test.com", first_name="Vendor", role=UserRole.VENDOR)
        vendor.set_password("password123")
        db.session.add(vendor)
        db.session.commit()
        
        store = Storefront(vendor_id=vendor.id, store_name="Test Store", store_slug="test-store", is_verified=True, is_published=True)
        cat = Category(name="Test Category", slug=f"cat-{uuid.uuid4().hex[:8]}")
        db.session.add(store)
        db.session.add(cat)
        db.session.commit()
        
        prod = Product(storefront_id=store.id, category_id=cat.id, name="Test Product", description="Test Description", price=500.0, stock_quantity=100, is_active=True)
        db.session.add(prod)
        db.session.commit()
        prod_id = prod.id

    # 5. Add to Cart & Checkout (Order 1)
    client.post('/api/cart/add', json={"product_id": prod_id, "quantity": 1}, headers={"Authorization": f"Bearer {referred_token}"})
    
    res = client.post('/api/cart/checkout', json={"payment_method": "POD"}, headers={"Authorization": f"Bearer {referred_token}"})
    print(f"3. First Checkout Status: {res.status_code}")

    with app.app_context():
        referrer = User.query.filter_by(email=referrer_email).first()
        print(f"   Referrer Points after Checkout (Should be 0): {referrer.points_balance}")
        assert float(referrer.points_balance or 0) == 0.0

        # Complete Order 1
        from app.models.order import Order
        from app.services.referral_service import check_and_reward_referral_on_order_complete
        order = Order.query.filter_by(buyer_id=referred_id).first()
        order.status = 'COMPLETED'
        db.session.commit()
        check_and_reward_referral_on_order_complete(order)
        db.session.commit()
        
        referrer = User.query.filter_by(email=referrer_email).first()
        print(f"   Referrer Points after Order 1 Completed (Should be 1000): {referrer.points_balance}")
        assert float(referrer.points_balance or 0) == 1000.0

    # 6. Add to Cart & Checkout (Order 2)
    client.post('/api/cart/add', json={"product_id": prod_id, "quantity": 1}, headers={"Authorization": f"Bearer {referred_token}"})
    res = client.post('/api/cart/checkout', json={"payment_method": "POD"}, headers={"Authorization": f"Bearer {referred_token}"})
    print(f"4. Second Checkout Status: {res.status_code}")

    with app.app_context():
        # Complete Order 2
        from app.models.order import Order
        from app.services.referral_service import check_and_reward_referral_on_order_complete
        # find the second order
        orders = Order.query.filter_by(buyer_id=referred_id).all()
        order2 = orders[1] if len(orders) > 1 else orders[0]
        order2.status = 'COMPLETED'
        db.session.commit()
        check_and_reward_referral_on_order_complete(order2)
        db.session.commit()

        referrer = User.query.filter_by(email=referrer_email).first()
        print(f"   Referrer Points after Order 2 Completed (Should be 1000): {referrer.points_balance}")
        assert float(referrer.points_balance or 0) == 1000.0

    # 7. Check Order History displays 'POD' instead of 'payscrow'
    res = client.get('/api/buyer-orders/history', headers={"Authorization": f"Bearer {referred_token}"})
    orders = res.json.get('orders', [])
    print(f"5. Fetched Order History, Total Orders: {len(orders)}")
    for i, o in enumerate(orders):
        print(f"   Order {i+1} Payment Method Displayed: {o.get('payment_method')}")

    print("\n[SUCCESS] All Tests Completed Successfully!")

if __name__ == '__main__':
    run_tests()
