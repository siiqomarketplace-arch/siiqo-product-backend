"""Only the buyer can release escrow. The seller's shared link can't do it on
its own; a guest buyer confirms with a code emailed to them. /escrow/status
keeps release links, codes, paid files and buyer contacts private."""
import re

import pytest

from app.extensions import db
from app.models.escrow import EscrowStatus, EscrowTransaction
from app.models.order import Order
from app.routes.escrow import generate_order_token


@pytest.fixture
def sent(monkeypatch):
    outbox = []
    import app.utils.email as email
    monkeypatch.setattr(email, "send_siiqo_email", lambda **k: outbox.append(k) or True)
    return outbox


@pytest.fixture
def shipped(make_vendor, make_order, monkeypatch):
    import app.routes.escrow as escrow_routes
    releases = []
    monkeypatch.setattr(escrow_routes, "execute_order_escrow_release",
                        lambda order, escrow, source=None: releases.append(source) or True)
    vendor, _ = make_vendor()
    order = make_order(vendor, total=5000)
    order.status = "SHIPPED"
    order.buyer_email = "buyer@example.org"
    db.session.add(EscrowTransaction(order_id=order.id, transaction_number=f"FLW-PL-{order.id}-AAAA",
                                     status=EscrowStatus.SHIPPED, amount=5000, escrow_code="482913"))
    db.session.commit()
    return vendor, order, releases


def _confirm(client, order, headers=None, **body):
    return client.post("/api/escrow/confirm-delivery-by-token",
                       json={"order_id": order.id, **body}, headers=headers or {})


def _code_from(outbox):
    return re.search(r"<strong>(\d{6})</strong>", outbox[-1]["notice_text"]).group(1)


def test_vendor_link_has_no_release_token(client, shipped, auth_headers):
    vendor, order, _ = shipped
    body = client.get("/api/vendor/orders", headers=auth_headers(vendor)).get_data(as_text=True)
    assert generate_order_token(order.id) not in body


def test_shared_link_alone_cannot_release(client, shipped, auth_headers):
    vendor, order, releases = shipped
    assert _confirm(client, order).status_code == 403
    assert _confirm(client, order, headers=auth_headers(vendor)).status_code == 403
    assert releases == []


def test_buyer_code_releases(client, shipped, sent):
    _, order, releases = shipped
    res = client.post("/api/escrow/confirm-delivery/send-code", json={"order_id": order.id})
    assert res.status_code == 200 and res.get_json()["sent_to"].endswith("@example.org")
    assert sent[-1]["to_email"] == "buyer@example.org"
    assert _confirm(client, order, code=_code_from(sent)).status_code == 200
    assert releases == ["guest_buyer_token"]


def test_wrong_codes_lock_out(client, shipped, sent):
    _, order, releases = shipped
    client.post("/api/escrow/confirm-delivery/send-code", json={"order_id": order.id})
    good = _code_from(sent)
    wrong = "000000" if good != "000000" else "111111"
    for _ in range(5):
        assert _confirm(client, order, code=wrong).status_code == 403
    assert _confirm(client, order, code=good).status_code == 403
    assert releases == []


def test_buyers_private_link_still_works(client, shipped):
    _, order, releases = shipped
    assert _confirm(client, order, token=generate_order_token(order.id)).status_code == 200
    assert releases


def test_placeholder_guest_email_gets_no_code(client, shipped, sent):
    _, order, _ = shipped
    order.buyer_email = f"guest-{order.id}@siiqo.com"
    db.session.commit()
    res = client.post("/api/escrow/confirm-delivery/send-code", json={"order_id": order.id})
    assert res.status_code == 409 and sent == []


def test_escrow_status_by_order_id_is_public_view(client, shipped):
    _, order, _ = shipped
    data = client.get(f"/api/escrow/status?order_id={order.id}").get_json()
    assert data["status"] == EscrowStatus.SHIPPED
    for key in ("confirmation_url", "escrow_code", "transaction_number", "buyer_email"):
        assert not data[key], key
    assert generate_order_token(order.id) not in str(data)


def test_escrow_status_with_payment_reference_is_private_view(client, shipped):
    _, order, _ = shipped
    ref = db.session.query(EscrowTransaction).filter_by(order_id=order.id).one().transaction_number
    data = client.get(f"/api/escrow/status?order_id={order.id}&tx_ref={ref}").get_json()
    assert generate_order_token(order.id) in data["confirmation_url"]
    assert data["escrow_code"] == "482913"


def test_shared_link_page_hides_buyer_contact(client, shipped):
    _, order, _ = shipped
    data = client.get(f"/api/escrow/order-details-by-token?order_id={order.id}").get_json()
    assert data["order"]["buyer_email"] is None
    assert data["verified_buyer"] is False
    assert data["code_email_hint"].endswith("@example.org")
    assert db.session.get(Order, order.id).status == "SHIPPED"
