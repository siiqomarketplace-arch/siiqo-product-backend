"""R1 / R2: logged-in cart add and quantity update."""
from app.extensions import db
from app.models.order import Cart, CartItem


def _cart_items(user):
    cart = Cart.query.filter_by(user_id=user.id).first()
    return CartItem.query.filter_by(cart_id=cart.id).all() if cart else []


def test_add_to_cart_succeeds(client, make_user, make_vendor, make_product, auth_headers):
    buyer = make_user()
    _, store = make_vendor()
    product = make_product(store, stock=5)

    res = client.post("/api/cart/add", json={"product_id": product.id, "quantity": 2},
                      headers=auth_headers(buyer))

    assert res.status_code == 200, res.get_json()
    items = _cart_items(buyer)
    assert len(items) == 1
    assert items[0].quantity == 2


def test_add_to_cart_accepts_string_product_id(client, make_user, make_vendor, make_product, auth_headers):
    buyer = make_user()
    _, store = make_vendor()
    product = make_product(store)

    res = client.post("/api/cart/add", json={"product_id": str(product.id)},
                      headers=auth_headers(buyer))

    assert res.status_code == 200, res.get_json()


def test_add_to_cart_requires_product_id(client, make_user, auth_headers):
    buyer = make_user()

    res = client.post("/api/cart/add", json={"quantity": 1}, headers=auth_headers(buyer))

    assert res.status_code == 400
    assert "product_id" in res.get_json()["message"]


def test_add_to_cart_rejects_bad_product_id(client, make_user, auth_headers):
    buyer = make_user()

    res = client.post("/api/cart/add", json={"product_id": "abc"}, headers=auth_headers(buyer))

    assert res.status_code == 400


def test_add_to_cart_rejects_bad_quantity(client, make_user, make_vendor, make_product, auth_headers):
    buyer = make_user()
    _, store = make_vendor()
    product = make_product(store)

    for qty in (0, -3, 1001, "two"):
        res = client.post("/api/cart/add", json={"product_id": product.id, "quantity": qty},
                          headers=auth_headers(buyer))
        assert res.status_code == 400, qty


def test_add_to_cart_respects_stock(client, make_user, make_vendor, make_product, auth_headers):
    buyer = make_user()
    _, store = make_vendor()
    product = make_product(store, stock=1)

    res = client.post("/api/cart/add", json={"product_id": product.id, "quantity": 2},
                      headers=auth_headers(buyer))

    assert res.status_code == 400
    assert _cart_items(buyer) == []


def test_vendor_cannot_add_own_product(client, make_vendor, make_product, auth_headers):
    vendor, store = make_vendor()
    product = make_product(store)

    res = client.post("/api/cart/add", json={"product_id": product.id},
                      headers=auth_headers(vendor))

    assert res.status_code == 403


def test_update_cart_quantity_succeeds(client, make_user, make_vendor, make_product, auth_headers):
    buyer = make_user()
    _, store = make_vendor()
    product = make_product(store, stock=10)
    client.post("/api/cart/add", json={"product_id": product.id}, headers=auth_headers(buyer))
    item = _cart_items(buyer)[0]

    res = client.patch(f"/api/cart/update/{item.id}", json={"quantity": 4},
                       headers=auth_headers(buyer))

    assert res.status_code == 200, res.get_json()
    db.session.expire_all()
    assert _cart_items(buyer)[0].quantity == 4


def test_update_cart_quantity_respects_stock(client, make_user, make_vendor, make_product, auth_headers):
    buyer = make_user()
    _, store = make_vendor()
    product = make_product(store, stock=3)
    client.post("/api/cart/add", json={"product_id": product.id}, headers=auth_headers(buyer))
    item = _cart_items(buyer)[0]

    res = client.patch(f"/api/cart/update/{item.id}", json={"quantity": 5},
                       headers=auth_headers(buyer))

    assert res.status_code == 400
    assert "3" in res.get_json()["message"]


def test_update_cart_quantity_rejects_bad_input(client, make_user, make_vendor, make_product, auth_headers):
    buyer = make_user()
    _, store = make_vendor()
    product = make_product(store)
    client.post("/api/cart/add", json={"product_id": product.id}, headers=auth_headers(buyer))
    item = _cart_items(buyer)[0]

    for body in ({}, {"quantity": None}, {"quantity": "x"}, {"quantity": 0}):
        res = client.patch(f"/api/cart/update/{item.id}", json=body, headers=auth_headers(buyer))
        assert res.status_code == 400, body
