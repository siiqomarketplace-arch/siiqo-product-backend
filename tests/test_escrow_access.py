"""M6: only the order's buyer (or an admin) can read the escrow delivery code."""
import pytest

from app.extensions import db
from app.models.escrow import EscrowTransaction
from app.models.user import UserRole


@pytest.fixture
def paid_order(make_user, make_vendor, make_order):
    buyer = make_user()
    vendor, _ = make_vendor()
    order = make_order(vendor, buyer=buyer, total=5000)
    db.session.add(EscrowTransaction(order_id=order.id, transaction_number="ESC-T1",
                                     status="IN_ESCROW", amount=5000, escrow_code="123456"))
    db.session.commit()
    return buyer, vendor, order


def test_buyer_can_read_own_code(client, paid_order, auth_headers):
    buyer, _, order = paid_order
    res = client.get(f"/api/buyer-orders/{order.id}/escrow-code", headers=auth_headers(buyer))
    assert res.status_code == 200
    assert res.get_json()["escrow_code"] == "123456"


def test_vendor_cannot_read_buyers_code(client, paid_order, auth_headers):
    _, vendor, order = paid_order
    res = client.get(f"/api/buyer-orders/{order.id}/escrow-code", headers=auth_headers(vendor))
    assert res.status_code == 404
    assert "123456" not in res.get_data(as_text=True)


def test_stranger_cannot_read_code(client, paid_order, make_user, auth_headers):
    _, _, order = paid_order
    stranger = make_user()
    res = client.get(f"/api/buyer-orders/{order.id}/escrow-code", headers=auth_headers(stranger))
    assert res.status_code == 404


def test_admin_user_can_read_code(client, paid_order, make_user, auth_headers):
    _, _, order = paid_order
    admin = make_user(role=UserRole.ADMIN)
    res = client.get(f"/api/buyer-orders/{order.id}/escrow-code", headers=auth_headers(admin))
    assert res.status_code == 200
