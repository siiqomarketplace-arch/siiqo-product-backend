"""Deleting an account anonymises it: personal details go, while orders,
escrow, reviews and trust evidence stay. Open orders block self-deletion."""
from flask_jwt_extended import create_access_token

from app.extensions import db
from app.models.admin import AdminUser
from app.models.escrow import EscrowStatus, EscrowTransaction
from app.models.order import Order
from app.models.telemetry import TrustEvidence
from app.models.user import Storefront, User


def _escrow(order, status):
    e = EscrowTransaction(order_id=order.id, transaction_number=f"T-{order.id}", status=EscrowStatus.PENDING_PAYMENT,
                          amount=1000, fee_amount=30)
    db.session.add(e)
    db.session.commit()
    e.status = status
    db.session.commit()
    return e


def _delete_self(client, user, auth_headers):
    return client.delete("/api/auth/delete-account", json={"password": "Password123!"}, headers=auth_headers(user))


def test_self_delete_anonymises_and_keeps_records(client, make_vendor, make_user, make_order, auth_headers):
    vendor, store = make_vendor()
    vendor.set_password("Password123!")
    vendor.phone = "08031234567"
    db.session.commit()
    buyer = make_user()
    order = make_order(vendor, buyer=buyer, total=1000)
    _escrow(order, EscrowStatus.RELEASED)

    res = _delete_self(client, vendor, auth_headers)
    assert res.status_code == 200, res.get_json()

    db.session.expire_all()
    u = db.session.get(User, vendor.id)
    assert u.email.startswith("deleted-") and u.phone is None and u.is_active is False
    assert not u.check_password("Password123!")
    sf = db.session.get(Storefront, store.id)
    assert sf.is_published is False and sf.store_name == "Closed store"
    assert db.session.get(Order, order.id) is not None                  # buyer keeps the order
    assert TrustEvidence.query.filter_by(business_id=vendor.id).count() > 0  # evidence kept


def test_open_order_blocks_self_delete(client, make_vendor, make_order, make_user, auth_headers):
    vendor, _ = make_vendor()
    buyer = make_user()
    buyer.set_password("Password123!")
    db.session.commit()
    _escrow(make_order(vendor, buyer=buyer, total=1000), EscrowStatus.IN_ESCROW)
    res = _delete_self(client, buyer, auth_headers)
    assert res.status_code == 409
    assert db.session.get(User, buyer.id).is_active is True


def test_admin_delete_keeps_orders(client, make_vendor, make_user, make_order):
    vendor, _ = make_vendor()
    buyer = make_user()
    order = make_order(vendor, buyer=buyer, total=1000)
    _escrow(order, EscrowStatus.RELEASED)
    admin = AdminUser(name="Ops", email="ops@example.com", role="SUPERADMIN")
    admin.set_password("not-a-real-password")
    db.session.add(admin)
    db.session.commit()
    headers = {"Authorization": f"Bearer {create_access_token(identity=f'admin:{admin.id}')}"}

    res = client.delete(f"/api/admin/users/{vendor.id}", headers=headers)
    assert res.status_code == 200, res.get_json()
    db.session.expire_all()
    assert db.session.get(Order, order.id) is not None
    assert EscrowTransaction.query.filter_by(order_id=order.id).count() == 1
    assert db.session.get(User, vendor.id).email.startswith("deleted-")
