"""Phase 0B: an order charged in another currency is paid out in naira at the
rate captured when the buyer paid. No rate (or a rate that fails the sanity
guard) means the payout is held exactly as in test_fx_hold.py."""
from decimal import Decimal
from types import SimpleNamespace

import pytest

from app.extensions import db
from app.models.escrow import EscrowStatus, EscrowTransaction
from app.models.finance import Ledger
from app.models.order import OrderItem
from app.models.telemetry import PlatformEvent
from app.models.withdrawal import DayaPayment, VendorBankAccount
from app.routes import payments as payments_routes
from app.routes.escrow import (_deliver_digital_products, _paystack_payout_vendor,
                               execute_order_escrow_release)
from app.routes.withdrawal import _get_ledger_balance
from app.services import daya_service, flutterwave_service, fx


@pytest.fixture(autouse=True)
def _no_email(monkeypatch):
    import app.utils.email as email
    monkeypatch.setattr(email, "send_siiqo_email", lambda *a, **k: True)


@pytest.fixture
def quote(monkeypatch):
    """Daya USDT/NGN quote; set .rate = None to make it unavailable."""
    state = SimpleNamespace(rate=1500, calls=0)

    def get_rate(**kwargs):
        state.calls += 1
        if state.rate is None:
            raise RuntimeError("quote unavailable")
        return {"rate": state.rate}
    monkeypatch.setenv("DAYA_API_KEY", "sk_test_dummy")
    monkeypatch.setattr(daya_service, "get_rate", get_rate)
    return state


@pytest.fixture
def flw_rate(monkeypatch):
    """Flutterwave transfer rate: NGN per 1 unit; set .ngn = None to make it unavailable."""
    state = SimpleNamespace(ngn=1500, calls=0)

    def get_transfer_rate(source_currency, destination_currency, amount):
        state.calls += 1
        if state.ngn is None:
            return {"success": False, "error_message": "unavailable"}
        return {"success": True, "rate": 1 / state.ngn,
                "source": {"currency": source_currency, "amount": amount / state.ngn},
                "destination": {"currency": destination_currency, "amount": amount}}
    monkeypatch.setenv("FLW_SECRET_KEY", "FLWSECK_TEST-dummy")
    monkeypatch.setattr(flutterwave_service, "get_transfer_rate", get_transfer_rate)
    return state


@pytest.fixture
def transfers(monkeypatch):
    calls = []

    def record(**kwargs):
        calls.append(kwargs)
        return {"success": True}
    monkeypatch.setattr(flutterwave_service, "transfer_to_vendor", record)
    monkeypatch.setattr(daya_service, "transfer_ngn_to_vendor", record)
    monkeypatch.setattr(daya_service, "get_merchant_balance",
                        lambda: {"data": {"collection_balance_usd": 0, "withdrawal_balance_usd": 100000}})
    return calls


def _order(make_vendor, make_product, make_order, currency, payment_method="FLUTTERWAVE",
           amount=100, fee=3, status=EscrowStatus.IN_ESCROW, product_type="digital", prefix="FLW-PL"):
    vendor, store = make_vendor()
    db.session.add(VendorBankAccount(vendor_id=vendor.id, bank_name="Test Bank", bank_code="000",
                                     account_number="0000000000", account_name="Test Vendor", is_default=True))
    product = make_product(store, price=amount, product_type=product_type, file_url="https://files.example/x.pdf")
    order = make_order(vendor, total=amount, payment_method=payment_method)
    db.session.add(OrderItem(order_id=order.id, product_id=product.id, price_at_purchase=amount, quantity=1))
    escrow = EscrowTransaction(order_id=order.id, transaction_number=f"{prefix}-{order.id}-AAAA",
                               status=status, amount=amount, fee_amount=fee, currency=currency)
    db.session.add(escrow)
    db.session.commit()
    return vendor, order, escrow


def _set_rate(order, rate):
    # Pay Link orders are created as NGN; the captured rate travels with the
    # escrow's currency (see fx.capture_rate).
    order.currency = EscrowTransaction.query.filter_by(order_id=order.id).one().currency
    order.fx_rate_to_ngn = rate
    db.session.commit()


def _ledger(vendor_id, prefix=None):
    rows = Ledger.query.filter_by(vendor_id=vendor_id).all()
    return [r for r in rows if prefix is None or (r.reference_id or "").startswith(prefix)]


# ── fx.naira_net ────────────────────────────────────────────────────────────

def test_naira_net_rounds_half_up_to_kobo():
    order = SimpleNamespace(currency="USD", fx_rate_to_ngn=Decimal("1.5"))
    escrow = SimpleNamespace(currency="USD", amount=Decimal("0.33"), fee_amount=Decimal("0.30"))
    # 0.03 * 1.5 = 0.045 -> 0.05 (banker's rounding would give 0.04)
    assert fx.naira_net(order, escrow) == Decimal("0.05")


def test_naira_net_none_for_ngn_unknown_rate_or_mismatched_currency():
    escrow = SimpleNamespace(currency="USD", amount=100, fee_amount=3)
    assert fx.naira_net(SimpleNamespace(currency="USD", fx_rate_to_ngn=None), escrow) is None
    assert fx.naira_net(SimpleNamespace(currency="USD", fx_rate_to_ngn=0), escrow) is None
    assert fx.naira_net(SimpleNamespace(currency="GBP", fx_rate_to_ngn=2000), escrow) is None
    ngn = SimpleNamespace(currency="NGN", amount=100, fee_amount=3)
    assert fx.naira_net(SimpleNamespace(currency="NGN", fx_rate_to_ngn=1), ngn) is None


# ── fx.capture_rate ─────────────────────────────────────────────────────────

def test_capture_prefers_gateway_rate_from_daya_amount(make_vendor, make_product, make_order, quote):
    quote.rate = 1480  # within the default 10% of the gateway rate
    _, order, escrow = _order(make_vendor, make_product, make_order, "USD", payment_method="CRYPTO")
    dp = SimpleNamespace(amount_ngn=Decimal("150000.00"))
    result = fx.capture_rate(order, escrow, dp)
    db.session.commit()
    assert result["source"] == fx.SOURCE_DAYA_AMOUNT
    assert Decimal(str(order.fx_rate_to_ngn)) == Decimal("1500")
    event = PlatformEvent.query.filter_by(order_id=order.id, event_name="fx_rate_captured").one()
    assert event.source == fx.SOURCE_DAYA_AMOUNT
    assert event.properties["quoted_rate"] == "1480"


def test_capture_uses_flutterwave_rate_for_flutterwave_usd(make_vendor, make_product, make_order, quote, flw_rate):
    quote.rate = 1480  # only a sanity check now
    _, order, escrow = _order(make_vendor, make_product, make_order, "USD")
    verification = {"success": True, "status": "successful", "currency": "USD", "amount": 103.0}
    result = fx.capture_rate(order, escrow, verification)
    db.session.commit()
    assert result["source"] == fx.SOURCE_FLW_RATE
    assert Decimal(str(order.fx_rate_to_ngn)) == Decimal("1500")


def test_flutterwave_order_never_uses_daya_quote_as_rate(make_vendor, make_product, make_order, quote, flw_rate):
    flw_rate.ngn = None
    _, order, escrow = _order(make_vendor, make_product, make_order, "USD")
    assert fx.capture_rate(order, escrow, {"currency": "USD"}) is None
    assert order.fx_rate_to_ngn is None


def test_flutterwave_rate_rejected_when_far_from_quote(make_vendor, make_product, make_order, quote, flw_rate):
    flw_rate.ngn = 2000  # 33% above the 1500 quote
    _, order, escrow = _order(make_vendor, make_product, make_order, "USD")
    assert fx.capture_rate(order, escrow, {"currency": "USD"}) is None
    assert PlatformEvent.query.filter_by(order_id=order.id, event_name="fx_rate_rejected").count() == 1


def test_paystack_order_never_uses_daya_quote_as_rate(make_vendor, make_product, make_order, quote):
    _, order, escrow = _order(make_vendor, make_product, make_order, "USD", payment_method="PAYSTACK", prefix="PL")
    assert fx.capture_rate(order, escrow) is None


def test_capture_leaves_rate_null_without_any_source(make_vendor, make_product, make_order, quote):
    # GBP has no quote source (USDT only tracks USD) and Flutterwave gives no rate
    _, order, escrow = _order(make_vendor, make_product, make_order, "GBP")
    assert fx.capture_rate(order, escrow, {"currency": "GBP"}) is None
    assert order.fx_rate_to_ngn is None
    assert quote.calls == 0


def test_capture_leaves_rate_null_when_quote_unavailable(make_vendor, make_product, make_order, quote):
    quote.rate = None
    _, order, escrow = _order(make_vendor, make_product, make_order, "USD")
    assert fx.capture_rate(order, escrow, {}) is None
    assert order.fx_rate_to_ngn is None


def test_capture_is_idempotent(make_vendor, make_product, make_order, quote, flw_rate):
    _, order, escrow = _order(make_vendor, make_product, make_order, "USD")
    fx.capture_rate(order, escrow)
    flw_rate.ngn = quote.rate = 1700
    assert fx.capture_rate(order, escrow)["source"] == "existing"
    assert Decimal(str(order.fx_rate_to_ngn)) == Decimal("1500")


def test_capture_rejects_rate_that_deviates_from_quote(make_vendor, make_product, make_order, quote, monkeypatch):
    monkeypatch.setenv("FX_MAX_DEVIATION", "0.05")
    quote.rate = 1400  # gateway 1500 is 7.1% away
    _, order, escrow = _order(make_vendor, make_product, make_order, "USD", payment_method="CRYPTO")
    assert fx.capture_rate(order, escrow, SimpleNamespace(amount_ngn=150000)) is None
    db.session.commit()
    assert order.fx_rate_to_ngn is None
    assert PlatformEvent.query.filter_by(order_id=order.id, event_name="fx_rate_rejected").count() == 1


def test_capture_ngn_is_noop(make_vendor, make_product, make_order, quote):
    _, order, escrow = _order(make_vendor, make_product, make_order, "NGN")
    assert fx.capture_rate(order, escrow)["rate"] == Decimal("1")
    assert quote.calls == 0


# ── payouts ─────────────────────────────────────────────────────────────────

def test_flutterwave_payout_sends_converted_naira_and_ledger_is_naira(
        make_vendor, make_product, make_order, transfers):
    vendor, order, escrow = _order(make_vendor, make_product, make_order, "USD")
    _set_rate(order, Decimal("1500.5"))
    _deliver_digital_products(order, escrow)
    db.session.commit()
    expected = 97 * 1500.5  # 145548.50
    assert len(transfers) == 1 and transfers[0]["amount_ngn"] == expected
    credit = _ledger(vendor.id, escrow.transaction_number)
    debits = _ledger(vendor.id, "PAYOUT-")
    assert [float(r.amount) for r in credit] == [expected]
    assert [float(r.amount) for r in debits] == [expected]  # one debit per dispatched payout
    assert _ledger(vendor.id, "FX-HOLD-") == []
    assert _get_ledger_balance(vendor.id) == Decimal("0")


def test_unknown_rate_still_holds(make_vendor, make_product, make_order, transfers):
    vendor, order, escrow = _order(make_vendor, make_product, make_order, "USD")
    _deliver_digital_products(order, escrow)
    db.session.commit()
    assert transfers == []
    assert len(_ledger(vendor.id, "FX-HOLD-")) == 1
    assert _get_ledger_balance(vendor.id) == Decimal("0")


def test_paystack_payout_sends_converted_naira(make_vendor, make_product, make_order, monkeypatch):
    from app.services.escrow import paystack_provider
    calls = []
    monkeypatch.setattr(paystack_provider, "paystack_transfer_to_vendor",
                        lambda **k: calls.append(k) or {"success": True})
    vendor, order, escrow = _order(make_vendor, make_product, make_order, "EUR", payment_method="PAYSTACK")
    VendorBankAccount.query.filter_by(vendor_id=vendor.id).update({"recipient_code": "RCP_test"})
    _set_rate(order, Decimal("1650"))
    assert _paystack_payout_vendor(order, escrow) is True
    assert calls[0]["amount_ngn"] == 97 * 1650


def test_daya_payout_sends_converted_naira(make_vendor, make_product, make_order, transfers, quote):
    _, order, escrow = _order(make_vendor, make_product, make_order, "GBP", payment_method="DAYA_BANK_TRANSFER", prefix="ESC")
    _set_rate(order, Decimal("2000"))
    assert payments_routes._payout_vendor_via_daya(order, escrow) is True
    assert transfers[0]["amount_ngn"] == 194000.0


def test_release_of_physical_fx_order_credits_and_pays_naira(
        make_vendor, make_product, make_order, transfers):
    vendor, order, escrow = _order(make_vendor, make_product, make_order, "USD",
                                   product_type="physical", status=EscrowStatus.DELIVERED)
    order.status = "DELIVERED"
    _set_rate(order, Decimal("1500"))
    assert execute_order_escrow_release(order, escrow, source="test") is True
    db.session.commit()
    assert transfers[0]["amount_ngn"] == 145500.0
    assert [float(r.amount) for r in _ledger(vendor.id, "PAYOUT-")] == [145500.0]
    assert _get_ledger_balance(vendor.id) == Decimal("0")


# ── confirmation paths capture the rate ─────────────────────────────────────

def test_flutterwave_confirmation_captures_rate_and_pays_naira(
        make_vendor, make_product, make_order, transfers, quote, flw_rate):
    vendor, order, escrow = _order(make_vendor, make_product, make_order, "USD",
                                   status=EscrowStatus.PENDING_PAYMENT)
    verification = {"success": True, "status": "successful", "currency": "USD",
                    "amount": 103.0, "tx_ref": escrow.transaction_number, "order_id": str(order.id)}
    payments_routes._handle_flutterwave_payment_confirmed(escrow.transaction_number, "987", verification)
    db.session.commit()
    db.session.refresh(order)
    assert Decimal(str(order.fx_rate_to_ngn)) == Decimal("1500")
    assert [t["amount_ngn"] for t in transfers] == [145500.0]
    assert _get_ledger_balance(vendor.id) == Decimal("0")


def test_daya_confirmation_captures_gateway_rate_and_pays_naira(
        make_vendor, make_product, make_order, transfers, quote):
    quote.rate = 1490
    vendor, order, escrow = _order(make_vendor, make_product, make_order, "USD",
                                   payment_method="DAYA_BANK_TRANSFER", status=EscrowStatus.PENDING_PAYMENT,
                                   prefix="ESC")
    dp = DayaPayment(order_id=order.id, payment_type="ngn_onramp", amount_ngn=150000,
                     status="COMPLETED", daya_deposit_id="dep_1")
    db.session.add(dp)
    db.session.commit()
    payments_routes._handle_crypto_payment_confirmed(order.id, dp)
    db.session.refresh(order)
    assert Decimal(str(order.fx_rate_to_ngn)) == Decimal("1500")
    assert [t["amount_ngn"] for t in transfers] == [145500.0]
    assert [float(r.amount) for r in _ledger(vendor.id, "PAYOUT-")] == [145500.0]
    assert _get_ledger_balance(vendor.id) == Decimal("0")


# ── currency sync, payout channel, late capture ─────────────────────────────

def test_new_escrow_with_only_order_id_sets_order_currency(make_vendor, make_order):
    vendor, _ = make_vendor()
    order = make_order(vendor, total=100, payment_method="FLUTTERWAVE")
    assert order.currency == "NGN" and Decimal(str(order.fx_rate_to_ngn)) == Decimal("1")
    order_id = order.id
    db.session.expire_all()  # escrow.order not loaded, as on a fresh request
    db.session.add(EscrowTransaction(order_id=order_id, transaction_number=f"FLW-PL-{order_id}-BBBB",
                                     status=EscrowStatus.PENDING_PAYMENT, amount=100, fee_amount=3,
                                     currency="USD"))
    db.session.commit()
    from app.models.order import Order
    fresh = db.session.get(Order, order_id)
    assert fresh.currency == "USD"
    assert fresh.fx_rate_to_ngn is None  # a naira rate of 1 is never kept for a USD order


def test_daya_order_with_flw_reference_is_paid_once_via_daya(
        make_vendor, make_product, make_order, transfers, quote):
    vendor, order, escrow = _order(make_vendor, make_product, make_order, "NGN",
                                   payment_method="DAYA_BANK_TRANSFER", status=EscrowStatus.PENDING_PAYMENT,
                                   prefix="FLW-ORD")
    dp = DayaPayment(order_id=order.id, payment_type="ngn_onramp", amount_ngn=100,
                     status="COMPLETED", daya_deposit_id="dep_2")
    db.session.add(dp)
    db.session.commit()
    payments_routes._handle_crypto_payment_confirmed(order.id, dp)
    assert len(transfers) == 1  # Daya only; no Flutterwave transfer as well
    assert _get_ledger_balance(vendor.id) == Decimal("0")


def test_release_without_rate_captures_daya_rate_late(make_vendor, make_product, make_order, transfers, quote):
    vendor, order, escrow = _order(make_vendor, make_product, make_order, "USD",
                                   payment_method="DAYA_BANK_TRANSFER", product_type="physical",
                                   status=EscrowStatus.DELIVERED, prefix="ESC")
    order.status = "DELIVERED"
    db.session.add(DayaPayment(order_id=order.id, payment_type="ngn_onramp", amount_ngn=150000,
                               status="COMPLETED", daya_deposit_id="dep_3"))
    db.session.commit()
    assert execute_order_escrow_release(order, escrow, source="admin") is True
    db.session.commit()
    assert Decimal(str(order.fx_rate_to_ngn)) == Decimal("1500")
    assert [t["amount_ngn"] for t in transfers] == [145500.0]
    event = PlatformEvent.query.filter_by(order_id=order.id, event_name="fx_rate_captured").one()
    assert event.properties["stage"] == "release"
    assert _ledger(vendor.id, "FX-HOLD-") == []
    assert _get_ledger_balance(vendor.id) == Decimal("0")


def test_admin_retry_payout_refuses_foreign_order_without_rate(client, make_vendor, make_product, make_order,
                                                                transfers):
    from flask_jwt_extended import create_access_token
    from app.models.admin import AdminUser
    admin = AdminUser(name="Root", email="root-fx@example.com", role="SUPERADMIN")
    admin.set_password("x" * 12)
    db.session.add(admin)
    _, order, escrow = _order(make_vendor, make_product, make_order, "GBP", payment_method="CRYPTO", prefix="ESC")
    order.status = "COMPLETED"
    db.session.commit()
    headers = {"Authorization": f"Bearer {create_access_token(identity=f'admin:{admin.id}')}"}
    res = client.post(f"/api/admin/payout/retry-order/{order.id}", headers=headers, json={})
    assert res.status_code == 409
    assert res.get_json()["code"] == "FX_RATE_REQUIRED"
    assert transfers == []
