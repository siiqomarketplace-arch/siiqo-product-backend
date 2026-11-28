"""The buyer's order list gives a service's booking link only once the order is paid
(like a digital file)."""
from app.extensions import db
from app.models.order import OrderItem


def test_booking_link_hidden_until_paid(client, make_user, make_vendor, make_product, make_order, auth_headers):
    buyer = make_user()
    vendor, store = make_vendor()
    product = make_product(store, price=3000, product_type="service", booking_link="https://book.example/x")
    order = make_order(vendor, buyer=buyer, total=3000, payment_method="FLUTTERWAVE")
    db.session.add(OrderItem(order_id=order.id, product_id=product.id, price_at_purchase=3000, quantity=1))
    db.session.commit()

    def link():
        rows = client.get("/api/buyer-orders/history", headers=auth_headers(buyer)).get_json()
        rows = rows.get("orders", rows) if isinstance(rows, dict) else rows
        mine = [o for o in rows if o["id"] == order.id][0]
        return mine["items"][0]["booking_link"]

    assert link() is None
    order.status = "COMPLETED"
    db.session.commit()
    assert link() == "https://book.example/x"
