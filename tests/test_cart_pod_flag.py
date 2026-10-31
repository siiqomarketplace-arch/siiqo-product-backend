"""GET /cart items say whether the seller may take Pay on Delivery (used by the bot checkout)."""
from app.services import entitlements


def _cart(client, make_user, make_vendor, make_product, auth_headers):
    buyer = make_user()
    vendor, store = make_vendor()
    product = make_product(store)
    client.post("/api/cart/add", json={"product_id": product.id, "quantity": 1}, headers=auth_headers(buyer))
    return client.get("/api/cart/", headers=auth_headers(buyer)).get_json()["items"], vendor


def test_free_seller_has_no_pod(client, make_user, make_vendor, make_product, auth_headers):
    items, _ = _cart(client, make_user, make_vendor, make_product, auth_headers)
    assert items[0]["vendor_allows_pod"] is False


def test_entitled_seller_has_pod(client, make_user, make_vendor, make_product, auth_headers, monkeypatch):
    calls = []
    monkeypatch.setattr(entitlements, "has_feature", lambda uid, key, now=None: calls.append((uid, key)) or True)
    items, vendor = _cart(client, make_user, make_vendor, make_product, auth_headers)
    assert items[0]["vendor_allows_pod"] is True
    assert (vendor.id, "pod") in calls
