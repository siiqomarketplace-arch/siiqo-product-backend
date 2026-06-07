import os
import json
from app import create_app, db
from app.models.user import User, Storefront
from app.models.product import Product
from flask_jwt_extended import create_access_token

def run_tests():
    app = create_app()
    app.config['TESTING'] = True
    client = app.test_client()

    print("=== STARTING INTEGRATION TESTS ===")

    with app.app_context():
        # Find storefront 19
        sf = db.session.get(Storefront, 19)
        if not sf:
            print("ERROR: Storefront 19 not found in database!")
            return
        
        # Find user owner
        user = db.session.get(User, sf.vendor_id)
        if not user:
            print(f"ERROR: Owner of Storefront 19 (User ID: {sf.vendor_id}) not found!")
            return

        print(f"Testing with User: {user.email} (ID: {user.id}), Storefront ID: {sf.id}")
        
        # Generate JWT token
        token = create_access_token(identity=user.id)
        headers = {
            "Authorization": f"Bearer {token}"
        }

        print("\n--- TEST 1: Get My Products ---")
        resp = client.get('/api/vendor/products/my-products', headers=headers)
        print("Status Code:", resp.status_code)
        if resp.status_code != 200:
            print("Failed Response:", resp.text)
            return
        my_products_data = resp.get_json()
        print(f"Loaded {len(my_products_data.get('products', []))} products.")

        print("\n--- TEST 2: Add a Product ---")
        # Simulate FormData payload matching the frontend's postForm format
        # Flask test client lets us pass a dict as data. If it has File/Binary or content_type is set,
        # it formats it as multipart/form-data.
        payload = {
            "name": "E2E Test Product",
            "description": "Integration test description",
            "category": "Uncategorized",
            "price": "1500",
            "quantity": "5",
            "condition": "Used - Good",
            "location": "Test Integration City",
            "latitude": "6.5244",
            "longitude": "3.3792",
            "is_negotiable": "true",
            "floor_price": "1200"
        }
        resp = client.post('/api/vendor/products/add', data=payload, headers=headers)
        print("Status Code:", resp.status_code)
        if resp.status_code not in (200, 201):
            print("Failed Response:", resp.text)
            return
        added_product = resp.get_json()
        # The add route returns the product inside "data" or root
        p_data = added_product.get('data') or added_product
        product_id = p_data.get('id')
        print(f"Successfully created product. ID: {product_id}")

        # Retrieve direct from DB to verify fields
        db.session.expire_all()
        p = db.session.get(Product, product_id)
        print("DB Verified Condition:", p.condition)
        print("DB Verified Location:", p.location)
        print("DB Verified Latitude:", p.latitude)
        print("DB Verified Longitude:", p.longitude)
        print("DB Verified Is Negotiable:", p.is_negotiable)
        print("DB Verified Floor Price:", p.floor_price)

        assert p.condition == "Used - Good", "Condition mismatch!"
        assert p.location == "Test Integration City", "Location mismatch!"
        assert p.is_negotiable is True, "Negotiable mismatch!"

        print("\n--- TEST 3: Edit/Update Product ---")
        update_payload = {
            "condition": "Used - Like New",
            "location": "Updated Test City",
            "price": "1800"
        }
        resp = client.patch(f'/api/vendor/products/update/{product_id}', data=update_payload, headers=headers)
        print("Status Code:", resp.status_code)
        if resp.status_code != 200:
            print("Failed Response:", resp.text)
            return
        
        # Verify DB updates
        db.session.expire_all()
        p = db.session.get(Product, product_id)
        print("Updated DB Condition:", p.condition)
        print("Updated DB Location:", p.location)
        print("Updated DB Price:", p.price)

        assert p.condition == "Used - Like New", "Condition update failed!"
        assert p.location == "Updated Test City", "Location update failed!"
        assert float(p.price) == 1800.0, "Price update failed!"

        print("\n--- TEST 4: Delete Product (Soft Delete) ---")
        resp = client.delete(f'/api/vendor/products/delete/{product_id}', headers=headers)
        print("Status Code:", resp.status_code)
        if resp.status_code != 200:
            print("Failed Response:", resp.text)
            return
        
        # Verify DB soft-delete flag
        db.session.expire_all()
        p = db.session.get(Product, product_id)
        print("DB is_deleted flag:", p.is_deleted)
        print("DB is_active flag:", p.is_active)
        assert p.is_deleted is True, "Product soft-delete failed!"
        assert p.is_active is False, "Product deactivate failed!"

        print("\n--- TEST 5: Verify my-products Excludes Deleted Product ---")
        resp = client.get('/api/vendor/products/my-products', headers=headers)
        products_list = resp.get_json().get('products', [])
        ids = [prod['id'] for prod in products_list]
        print("Active Product IDs:", ids)
        assert product_id not in ids, "Deleted product was returned in active list!"
        print("✓ Verified deleted product is excluded.")

        print("\n=== ALL INTEGRATION TESTS PASSED SUCCESSFULLY ===")

if __name__ == '__main__':
    run_tests()
