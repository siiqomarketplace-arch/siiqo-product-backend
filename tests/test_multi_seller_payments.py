"""One card payment for a cart with several sellers (Paystack and Flutterwave).

Each order keeps its own unique escrow transaction_number; the gateway
reference is stored on every escrow as payment_ref. Confirmations find ALL
orders of the payment, mark each paid once, and each seller is paid once:
by the gateway split (MULTI_SPLIT_ENABLED, every seller has a subaccount) or
by Siiqo's transfer at release. Every gateway call is mocked."""
import hashlib
import hmac
import json
from decimal import Decimal

import pytest

from app.extensions import db
from app.models.escrow import EscrowStatus, EscrowTransaction
from app.models.finance import Ledger
from app.models.order import OrderItem
from app.models.withdrawal import VendorBankAccount
from app.routes import bridge
from app.routes.withdrawal import _get_ledger_balance
from app.services import flutterwave_service, payment_refs, split_settlement
from app.services.escrow import paystack_provider

PSK = "sk_test_not_a_real_key"


class _Resp:
    def __init__(self, data):
        self._data = data

    def json(self):
        return self._data


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    import app.utils.email as email
    monkeypatch.setattr(email, "send_siiqo_email", lambda *a, **k: True)
    monkeypatch.setenv("PAYSTACK_SECRET_KEY", PSK)
    monkeypatch.setenv("FLW_SECRET_KEY", "flw_test_not_a_real_key")
    monkeypatch.setattr(bridge, "PAYSTACK_SECRET_KEY", PSK)
    monkeypatch.delenv("MULTI_SPLIT_ENABLED", raising=False)


@pytest.fixture
def paystack_http(monkeypatch):
    """Mocked Paystack HTTP: /split and /transaction/initialize."""
    state = {"calls": [], "split_ok": True, "init_with_split_ok": True}

    def _post(url, json=None, headers=None, timeout=None):
        state["calls"].append((url, json))
        if url.endswith("/split"):
            if not state["split_ok"]:
                return _Resp({"status": False, "message": "mocked failure"})
            return _Resp({"status": True, "data": {"split_code": "SPL_test1"}})
        if url.endswith("/transaction/initialize"):
            if "split_code" in (json or {}) and not state["init_with_split_ok"]:
                return _Resp({"status": False, "message": "mocked split refused"})
            return _Resp({"status": True, "data": {"authorization_url": "https://pay.example/psk",
                                                   "access_code": "AC_test"}})
        raise AssertionError(f"unexpected Paystack call {url}")

    monkeypatch.setattr(paystack_provider.requests, "post", _post)
    return state


@pytest.fixture
def flw_http(monkeypatch):
    state = {"payloads": [], "split_ok": True}

    def _post(url, json=None, headers=None, timeout=None):
        assert url.endswith("/payments"), url
        state["payloads"].append(json)
        if "subaccounts" in json and len(json["subaccounts"]) > 1 and not state["split_ok"]:
            return _Resp({"status": "error", "message": "mocked split refused"})
        return _Resp({"status": "success", "data": {"link": "https://pay.example/flw"}})

    monkeypatch.setattr(flutterwave_service.requests, "post", _post)
    return state


@pytest.fixture
def transfers(monkeypatch):
    """Seller transfers by Siiqo (both gateways). Always succeed."""
    calls = []

    def _psk(**kwargs):
        calls.append(("paystack", kwargs["amount_ngn"]))
        return {"success": True, "transfer_code": "TRF_test"}

    def _flw(**kwargs):
        calls.append(("flutterwave", kwargs["amount_ngn"]))
        return {"success": True}

    monkeypatch.setattr(paystack_provider, "paystack_transfer_to_vendor", _psk)
    monkeypatch.setattr(flutterwave_service, "transfer_to_vendor", _flw)
    return calls


def _seller(make_vendor, psk_sub=True, flw_sub=True):
    vendor, store = make_vendor()
    db.session.add(VendorBankAccount(
        vendor_id=vendor.id, bank_name="Test Bank", bank_code="000", account_number="0000000000",
        account_name="Test Vendor", is_default=True, recipient_code=f"RCP_{vendor.id}",
        paystack_subaccount_code=f"ACCT_{vendor.id}" if psk_sub else None,
        flw_subaccount_id=f"RS_{vendor.id}" if flw_sub else None))
    db.session.commit()
    return vendor, store


def _cart(make_vendor, make_user, make_product, make_order, n, method, missing_sub=False):
    buyer = make_user()
    orders = []
    for i in range(n):
        has_sub = not (missing_sub and i == n - 1)
        vendor, store = _seller(make_vendor, psk_sub=has_sub, flw_sub=has_sub)
        price = 10000 * (i + 1)
        product = make_product(store, price=price, product_type="digital", file_url="https://files.example/x.pdf")
        order = make_order(vendor, buyer, total=price, payment_method=method)
        db.session.add(OrderItem(order_id=order.id, product_id=product.id, price_at_purchase=price, quantity=1))
        orders.append(order)
    db.session.commit()
    return buyer, orders


def _escrows(orders):
    return EscrowTransaction.query.filter(EscrowTransaction.order_id.in_([o.id for o in orders])) \
        .order_by(EscrowTransaction.order_id).all()


def _paystack_webhook(client, ref, orders):
    event = {"event": "charge.success", "data": {
        "reference": ref, "status": "success", "amount": 100,
        "customer": {"email": "buyer@example.com"},
        "metadata": {"order_ids": [str(o.id) for o in orders], "source": "marketplace_checkout"}}}
    body = json.dumps(event).encode()
    sig = hmac.new(PSK.encode(), body, hashlib.sha512).hexdigest()
    return client.post("/api/payments/webhook", data=body,
                       headers={"Content-Type": "application/json", "x-paystack-signature": sig})


def _credits(orders):
    return Ledger.query.filter(Ledger.transaction_type == "CREDIT",
                               Ledger.vendor_id.in_([o.vendor_id for o in orders])).count()


# ── Paystack ────────────────────────────────────────────────────────────────
@pytest.mark.parametrize("n", [2, 3])
@pytest.mark.parametrize("split_on", [False, True])
def test_paystack_one_payment_many_sellers(client, make_vendor, make_user, make_product, make_order,
                                           auth_headers, paystack_http, transfers, monkeypatch, n, split_on):
    if split_on:
        monkeypatch.setenv("MULTI_SPLIT_ENABLED", "true")
    buyer, orders = _cart(make_vendor, make_user, make_product, make_order, n, "PAYSTACK")
    res = client.post("/api/escrow/initiate", headers=auth_headers(buyer),
                      json={"orderId": ",".join(str(o.id) for o in orders)})
    assert res.status_code == 200, res.get_json()
    body = res.get_json()
    assert set(body) == {"success", "paymentLink", "transactionNumber", "amount", "status"}
    ref = body["transactionNumber"]

    escrows = _escrows(orders)
    assert len({e.transaction_number for e in escrows}) == n
    assert all(e.payment_ref == ref for e in escrows)
    assert all(e.split_settled is split_on for e in escrows)

    init = [p for u, p in paystack_http["calls"] if u.endswith("/transaction/initialize")][0]
    if split_on:
        split = [p for u, p in paystack_http["calls"] if u.endswith("/split")][0]
        assert split["type"] == "flat" and split["bearer_type"] == "account"
        # Each seller's share = order total - 3% fee, in kobo
        assert sorted(s["share"] for s in split["subaccounts"]) == \
            sorted(int(float(o.total_amount) * 0.97 * 100) for o in orders)
        assert sum(s["share"] for s in split["subaccounts"]) <= init["amount"]
        assert init["split_code"] == "SPL_test1"
    else:
        assert not any(u.endswith("/split") for u, _ in paystack_http["calls"])
        assert "split_code" not in init and "subaccount" not in init

    assert _paystack_webhook(client, ref, orders).status_code == 200
    db.session.expire_all()
    escrows = _escrows(orders)
    assert all(e.status == EscrowStatus.RELEASED for e in escrows)
    assert len(transfers) == (0 if split_on else n)
    credits = _credits(orders)
    assert credits == n
    for o in orders:
        assert _get_ledger_balance(o.vendor_id) == Decimal("0")  # paid once, by split or transfer

    # A repeated webhook does nothing.
    assert _paystack_webhook(client, ref, orders).status_code == 200
    db.session.expire_all()
    assert len(transfers) == (0 if split_on else n)
    assert _credits(orders) == credits


def test_paystack_status_check_activates_every_order(client, make_vendor, make_user, make_product,
                                                     make_order, auth_headers, paystack_http, transfers,
                                                     monkeypatch):
    buyer, orders = _cart(make_vendor, make_user, make_product, make_order, 2, "PAYSTACK")
    ref = client.post("/api/escrow/initiate", headers=auth_headers(buyer),
                      json={"orderId": ",".join(str(o.id) for o in orders)}).get_json()["transactionNumber"]
    verified = []
    monkeypatch.setattr(paystack_provider.PaystackProvider, "verify_transaction",
                        lambda self, r: verified.append(r) or {"success": True, "status": "success"})
    assert client.get(f"/api/escrow/status?reference={ref}").status_code == 200
    assert verified == [ref]
    db.session.expire_all()
    assert all(e.status == EscrowStatus.RELEASED for e in _escrows(orders))
    assert len(transfers) == 2
    client.get(f"/api/escrow/status?reference={ref}")
    assert len(transfers) == 2


def test_paystack_retry_reuses_reference_and_paid_orders_cannot_restart(
        client, make_vendor, make_user, make_product, make_order, auth_headers, paystack_http, transfers):
    buyer, orders = _cart(make_vendor, make_user, make_product, make_order, 2, "PAYSTACK")
    ids = ",".join(str(o.id) for o in orders)
    ref1 = client.post("/api/escrow/initiate", headers=auth_headers(buyer), json={"orderId": ids}).get_json()["transactionNumber"]
    ref2 = client.post("/api/escrow/initiate", headers=auth_headers(buyer), json={"orderId": ids}).get_json()["transactionNumber"]
    assert ref1 == ref2
    _paystack_webhook(client, ref1, orders)
    res = client.post("/api/escrow/initiate", headers=auth_headers(buyer), json={"orderId": ids})
    assert res.status_code == 409


def test_paystack_retry_of_unsplit_payment_never_adds_split(
        client, make_vendor, make_user, make_product, make_order, auth_headers, paystack_http, monkeypatch):
    buyer, orders = _cart(make_vendor, make_user, make_product, make_order, 2, "PAYSTACK")
    ids = ",".join(str(o.id) for o in orders)
    client.post("/api/escrow/initiate", headers=auth_headers(buyer), json={"orderId": ids})
    monkeypatch.setenv("MULTI_SPLIT_ENABLED", "true")  # switched on between attempts
    client.post("/api/escrow/initiate", headers=auth_headers(buyer), json={"orderId": ids})
    assert not any(u.endswith("/split") for u, _ in paystack_http["calls"])
    assert all(e.split_settled is False for e in _escrows(orders))


# ── Paystack multi-split unit cases ─────────────────────────────────────────
def _orders_for_split(make_vendor, make_user, make_product, make_order, missing_sub=False):
    _, orders = _cart(make_vendor, make_user, make_product, make_order, 2, "PAYSTACK", missing_sub=missing_sub)
    return orders


def test_split_switch_off(make_vendor, make_user, make_product, make_order, paystack_http):
    orders = _orders_for_split(make_vendor, make_user, make_product, make_order)
    assert paystack_provider.multi_split_code_for(orders, "ORD-X") is None
    assert paystack_http["calls"] == []


def test_split_missing_subaccount(make_vendor, make_user, make_product, make_order, paystack_http, monkeypatch):
    monkeypatch.setenv("MULTI_SPLIT_ENABLED", "true")
    orders = _orders_for_split(make_vendor, make_user, make_product, make_order, missing_sub=True)
    assert paystack_provider.multi_split_code_for(orders, "ORD-X") is None
    assert paystack_http["calls"] == []


def test_split_api_failure(make_vendor, make_user, make_product, make_order, paystack_http, monkeypatch):
    monkeypatch.setenv("MULTI_SPLIT_ENABLED", "true")
    paystack_http["split_ok"] = False
    orders = _orders_for_split(make_vendor, make_user, make_product, make_order)
    result = paystack_provider.PaystackProvider().initiate_transaction(orders, "ORD-FAIL")
    assert result["success"] and result["used_split"] is False
    init = [p for u, p in paystack_http["calls"] if u.endswith("/transaction/initialize")][0]
    assert "split_code" not in init


def test_split_refused_at_initialize_falls_back(make_vendor, make_user, make_product, make_order,
                                                paystack_http, monkeypatch):
    monkeypatch.setenv("MULTI_SPLIT_ENABLED", "true")
    paystack_http["init_with_split_ok"] = False
    orders = _orders_for_split(make_vendor, make_user, make_product, make_order)
    result = paystack_provider.PaystackProvider().initiate_transaction(orders, "ORD-REF")
    assert result["success"] and result["used_split"] is False


def test_split_all_sellers(make_vendor, make_user, make_product, make_order, paystack_http, monkeypatch):
    monkeypatch.setenv("MULTI_SPLIT_ENABLED", "true")
    orders = _orders_for_split(make_vendor, make_user, make_product, make_order)
    result = paystack_provider.PaystackProvider().initiate_transaction(orders, "ORD-ALL")
    assert result["used_split"] is True and result["split_code"] == "SPL_test1"
    split = paystack_http["calls"][0][1]
    assert {s["subaccount"] for s in split["subaccounts"]} == {f"ACCT_{o.vendor_id}" for o in orders}
    assert sum(s["share"] for s in split["subaccounts"]) == 970000 + 1940000


# ── Flutterwave ─────────────────────────────────────────────────────────────
def _flw_webhook(client, monkeypatch, ref, amount, tx_id=777):
    monkeypatch.setattr(flutterwave_service, "verify_webhook_signature", lambda *a: True)
    monkeypatch.setattr(flutterwave_service, "verify_transaction", lambda tid: {
        "success": True, "status": "successful", "tx_ref": ref, "amount": amount,
        "currency": "NGN", "order_id": None})
    return client.post("/api/payments/flutterwave/webhook", json={
        "event": "charge.completed", "data": {"id": tx_id, "tx_ref": ref, "status": "successful"}},
        headers={"verif-hash": "mocked"})


@pytest.mark.parametrize("n", [2, 3])
@pytest.mark.parametrize("split_on", [False, True])
def test_flutterwave_one_payment_many_sellers(client, make_vendor, make_user, make_product, make_order,
                                              auth_headers, flw_http, transfers, monkeypatch, n, split_on):
    if split_on:
        monkeypatch.setenv("MULTI_SPLIT_ENABLED", "true")
    buyer, orders = _cart(make_vendor, make_user, make_product, make_order, n, "FLUTTERWAVE")
    res = client.post("/api/payments/flutterwave/initiate", headers=auth_headers(buyer),
                      json={"orderId": ",".join(str(o.id) for o in orders), "buyerEmail": "buyer@example.com"})
    assert res.status_code == 200, res.get_json()
    body = res.get_json()
    ref = body["transactionNumber"]
    assert set(body) == {"success", "paymentLink", "transactionNumber", "amount", "listedAmount", "flwFee", "orderId"}

    escrows = _escrows(orders)
    assert len({e.transaction_number for e in escrows}) == n
    assert all(e.payment_ref == ref for e in escrows)
    payload = flw_http["payloads"][0]
    if split_on:
        subs = payload["subaccounts"]
        assert {s["id"] for s in subs} == {f"RS_{o.vendor_id}" for o in orders}
        assert all(s["transaction_charge_type"] == "flat" for s in subs)
        assert sum(s["transaction_charge"] for s in subs) == pytest.approx(sum(float(o.total_amount) * 0.03 for o in orders))
        # ratios follow each seller's net (10k, 20k, 30k orders -> 1:2:3)
        assert [s["transaction_split_ratio"] for s in subs] == list(range(1, n + 1))
        assert all(e.payscrow_ref == f"SPLIT:RS_{e.order.vendor_id}:{ref}" for e in escrows)
    else:
        assert "subaccounts" not in payload
        assert all(e.payscrow_ref == f"DIRECT:{ref}" for e in escrows)

    total = float(body["amount"])
    assert _flw_webhook(client, monkeypatch, ref, total).status_code == 200
    db.session.expire_all()
    assert all(e.status == EscrowStatus.RELEASED for e in _escrows(orders))
    assert len(transfers) == (0 if split_on else n)
    credits = _credits(orders)
    assert credits == n
    for o in orders:
        assert _get_ledger_balance(o.vendor_id) == Decimal("0")

    assert _flw_webhook(client, monkeypatch, ref, total).status_code == 200
    db.session.expire_all()
    assert len(transfers) == (0 if split_on else n)
    assert _credits(orders) == credits


def test_flutterwave_missing_subaccount_means_no_split(client, make_vendor, make_user, make_product, make_order,
                                                       auth_headers, flw_http, monkeypatch):
    monkeypatch.setenv("MULTI_SPLIT_ENABLED", "true")
    buyer, orders = _cart(make_vendor, make_user, make_product, make_order, 2, "FLUTTERWAVE", missing_sub=True)
    res = client.post("/api/payments/flutterwave/initiate", headers=auth_headers(buyer),
                      json={"orderId": ",".join(str(o.id) for o in orders), "buyerEmail": "buyer@example.com"})
    assert res.status_code == 200
    assert "subaccounts" not in flw_http["payloads"][0]
    assert all(e.payscrow_ref.startswith("DIRECT:") for e in _escrows(orders))


def test_flutterwave_split_refused_falls_back(client, make_vendor, make_user, make_product, make_order,
                                              auth_headers, flw_http, monkeypatch):
    monkeypatch.setenv("MULTI_SPLIT_ENABLED", "true")
    flw_http["split_ok"] = False
    buyer, orders = _cart(make_vendor, make_user, make_product, make_order, 2, "FLUTTERWAVE")
    res = client.post("/api/payments/flutterwave/initiate", headers=auth_headers(buyer),
                      json={"orderId": ",".join(str(o.id) for o in orders), "buyerEmail": "buyer@example.com"})
    assert res.status_code == 200
    assert "subaccounts" not in flw_http["payloads"][-1]
    assert all(not split_settlement.flutterwave_split_settled(e) for e in _escrows(orders))


def test_flutterwave_status_needs_the_whole_payment_amount(client, make_vendor, make_user, make_product,
                                                           make_order, auth_headers, flw_http, transfers,
                                                           monkeypatch):
    buyer, orders = _cart(make_vendor, make_user, make_product, make_order, 2, "FLUTTERWAVE")
    ref = client.post("/api/payments/flutterwave/initiate", headers=auth_headers(buyer),
                      json={"orderId": ",".join(str(o.id) for o in orders),
                            "buyerEmail": "buyer@example.com"}).get_json()["transactionNumber"]
    # Paid only enough for the first order: nothing is confirmed.
    monkeypatch.setattr(flutterwave_service, "verify_transaction", lambda tid: {
        "success": True, "status": "successful", "tx_ref": ref, "amount": 10000.0, "currency": "NGN"})
    client.get(f"/api/payments/flutterwave/status?reference={ref}&transaction_id=5")
    db.session.expire_all()
    assert all(e.status == EscrowStatus.PENDING_PAYMENT for e in _escrows(orders))
    # Full amount: every order confirmed.
    monkeypatch.setattr(flutterwave_service, "verify_transaction", lambda tid: {
        "success": True, "status": "successful", "tx_ref": ref, "amount": 31000.0, "currency": "NGN"})
    client.get(f"/api/payments/flutterwave/status?reference={ref}&transaction_id=5")
    db.session.expire_all()
    assert all(e.status == EscrowStatus.RELEASED for e in _escrows(orders))
    assert len(transfers) == 2


# ── Old rows (no payment_ref) ───────────────────────────────────────────────
def test_old_row_found_by_transaction_number(make_vendor, make_order):
    vendor, _ = make_vendor()
    order = make_order(vendor, total=5000, payment_method="PAYSTACK")
    e = EscrowTransaction(order_id=order.id, transaction_number="ORD-OLDROW", status=EscrowStatus.PENDING_PAYMENT,
                          amount=5000, fee_amount=150)
    db.session.add(e)
    db.session.commit()
    assert payment_refs.escrows_for_payment("ORD-OLDROW") == [e]
    assert payment_refs.gateway_ref(e) == "ORD-OLDROW"


def test_old_row_paystack_webhook(client, make_vendor, make_user, make_product, make_order, transfers):
    buyer, orders = _cart(make_vendor, make_user, make_product, make_order, 1, "PAYSTACK")
    e = EscrowTransaction(order_id=orders[0].id, transaction_number="ORD-OLDWH", status=EscrowStatus.PENDING_PAYMENT,
                          amount=10000, fee_amount=300, split_settled=False)
    db.session.add(e)
    db.session.commit()
    _paystack_webhook(client, "ORD-OLDWH", orders)
    db.session.expire_all()
    assert db.session.get(EscrowTransaction, e.id).status == EscrowStatus.RELEASED
    assert len(transfers) == 1


def test_split_grouping_uses_payment_ref(make_vendor, make_order):
    a, _ = make_vendor()
    b, _ = make_vendor()
    oa = make_order(a, total=1000, payment_method="PAYSTACK")
    ob = make_order(b, total=1000, payment_method="PAYSTACK")
    ea = EscrowTransaction(order_id=oa.id, transaction_number="R1-O1", payment_ref="R1", amount=1000, fee_amount=30)
    eb = EscrowTransaction(order_id=ob.id, transaction_number="R1-O2", payment_ref="R1", amount=1000, fee_amount=30)
    db.session.add_all([ea, eb])
    db.session.commit()
    assert split_settlement.transaction_vendor_ids(ea, oa) == {a.id, b.id}
