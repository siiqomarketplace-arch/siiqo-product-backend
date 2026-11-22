"""Payout account holder names come only from a bank lookup (Daya, then
Paystack). Never the bank's name, store name or a typed name; when no name is
confirmed the account is "name pending" and payouts wait in the balance."""
from decimal import Decimal

import pytest
from datetime import datetime
from flask_jwt_extended import create_access_token

from app.extensions import db
from app.models.admin import AdminUser
from app.models.communication import Notification
from app.models.escrow import EscrowStatus, EscrowTransaction
from app.models.withdrawal import VendorBankAccount
from app.routes import escrow as escrow_routes
from app.routes import payments as payments_routes
from app.routes.escrow import _credit_vendor_ledger
from app.routes.withdrawal import _get_ledger_balance
from app.services import bank_resolve, daya_service
from app.services import payout_account_name as svc
from app.services.bank_resolve import BankResolveResult
from app.services.escrow import paystack_provider

ACCT = "7035360986"
BANK = "Moniepoint Microfinance Bank"


@pytest.fixture(autouse=True)
def _no_email(monkeypatch):
    import app.utils.email as email
    monkeypatch.setattr(email, "send_siiqo_email", lambda *a, **k: True)


@pytest.fixture
def lookups(monkeypatch):
    """Daya and Paystack lookups, mocked. Set state["daya"] / state["paystack"]
    to a name, "fail" (unavailable) or "reject"."""
    state = {"daya": "fail", "paystack": "fail", "daya_calls": 0, "paystack_calls": 0}
    monkeypatch.setenv("DAYA_API_KEY", "sk_sandbox_dummy")

    def fake_daya(bank_code, account_number):
        state["daya_calls"] += 1
        if state["daya"] in ("fail", "reject"):
            raise RuntimeError("Daya error (502): upstream")
        return {"account_name": state["daya"]}

    def fake_paystack(bank_code, account_number, timeout=10):
        state["paystack_calls"] += 1
        v = state["paystack"]
        if v == "fail":
            return BankResolveResult(status="unavailable", account_name=None, message="down")
        if v == "reject":
            return BankResolveResult(status="rejected", account_name=None, message="no match")
        return BankResolveResult(status="resolved", account_name=v, message="ok")

    monkeypatch.setattr(daya_service, "resolve_bank_account", fake_daya)
    monkeypatch.setattr(bank_resolve, "resolve_account", fake_paystack)
    return state


@pytest.fixture
def recipients(monkeypatch):
    """Paystack recipient creation: records the names it was asked to use."""
    calls = []
    monkeypatch.setenv("PAYSTACK_SECRET_KEY", "sk_test_dummy")

    class _Resp:
        def __init__(self, body):
            self.body = body

        def json(self):
            return self.body

    def fake_post(url, json=None, headers=None, timeout=None):
        if not url.endswith("/transferrecipient"):
            return _Resp({"status": False, "message": "not used in this test"})
        calls.append(json["name"])
        return _Resp({"status": True, "data": {"recipient_code": f"RCP_{len(calls)}"}})
    monkeypatch.setattr(paystack_provider.requests, "post", fake_post)
    return calls


def _register(client, vendor, auth_headers):
    return client.post("/api/withdrawal/daya-bank-account", headers=auth_headers(vendor),
                       json={"bank_code": "50515", "account_number": ACCT, "bank_name": BANK})


# ── Saving an account ─────────────────────────────────────────────────────────

def test_daya_fails_paystack_resolves_saves_real_name(client, make_vendor, auth_headers, lookups, recipients):
    vendor, _ = make_vendor()
    lookups["paystack"] = "OKEREKE INNOCENT C."
    res = _register(client, vendor, auth_headers)
    assert res.status_code == 201, res.get_json()
    acc = VendorBankAccount.query.filter_by(vendor_id=vendor.id).one()
    assert acc.account_name == "OKEREKE INNOCENT C." and acc.is_verified is True
    assert lookups["daya_calls"] >= 1 and lookups["paystack_calls"] >= 1
    assert recipients == ["OKEREKE INNOCENT C."] and acc.recipient_code
    assert res.get_json()["account"]["name_status"] == "verified"


def test_daya_name_used_first(client, make_vendor, auth_headers, lookups, recipients):
    vendor, _ = make_vendor()
    lookups["daya"] = "ADA OBI"
    lookups["paystack"] = "SHOULD NOT BE USED"
    assert _register(client, vendor, auth_headers).status_code == 201
    acc = VendorBankAccount.query.filter_by(vendor_id=vendor.id).one()
    assert acc.account_name == "ADA OBI"


def test_both_fail_saves_name_pending_without_recipient(client, make_vendor, auth_headers, lookups, recipients):
    vendor, _ = make_vendor()
    res = _register(client, vendor, auth_headers)
    assert res.status_code == 201
    acc = VendorBankAccount.query.filter_by(vendor_id=vendor.id).one()
    assert acc.account_name is None and acc.is_verified is False
    assert acc.recipient_code is None and recipients == []
    assert res.get_json()["account"]["name_status"] == "pending"
    assert BANK not in (acc.account_name or "")


def test_bank_name_from_a_lookup_is_never_saved(client, make_vendor, auth_headers, lookups, recipients):
    vendor, _ = make_vendor()
    lookups["daya"] = BANK          # a misbehaving lookup returning the bank's name
    lookups["paystack"] = "Moniepoint MFB"
    assert _register(client, vendor, auth_headers).status_code == 201
    acc = VendorBankAccount.query.filter_by(vendor_id=vendor.id).one()
    assert acc.account_name is None and acc.is_verified is False and recipients == []


def test_rejected_account_is_refused(client, make_vendor, auth_headers, lookups, recipients):
    vendor, _ = make_vendor()
    lookups["paystack"] = "reject"
    res = _register(client, vendor, auth_headers)
    assert res.status_code == 400
    assert "couldn't confirm this account" in res.get_json()["message"]
    assert VendorBankAccount.query.count() == 0


def test_paystack_add_path_has_no_bank_name_placeholder(client, make_vendor, auth_headers, lookups, recipients):
    vendor, _ = make_vendor()
    res = client.post("/api/withdrawal/bank-accounts", headers=auth_headers(vendor),
                      json={"bank_code": "50515", "account_number": ACCT, "bank_name": BANK})
    assert res.status_code == 201, res.get_json()
    acc = VendorBankAccount.query.filter_by(vendor_id=vendor.id).one()
    assert acc.account_name is None and acc.is_verified is False and recipients == []


def test_ensure_recipient_never_uses_a_bank_name(lookups, recipients):
    res = paystack_provider.ensure_paystack_transfer_recipient(
        account_number=ACCT, bank_code="50515", account_name=BANK)
    assert res["success"] is False and res["name_confirmed"] is False and recipients == []
    lookups["paystack"] = "OKEREKE INNOCENT C."
    res = paystack_provider.ensure_paystack_transfer_recipient(
        account_number=ACCT, bank_code="50515", account_name=BANK)
    assert res["success"] and res["name_confirmed"] and recipients == ["OKEREKE INNOCENT C."]


def test_storefront_sync_creates_name_pending_row(client, make_vendor, auth_headers):
    vendor, store = make_vendor()
    store.bank_code, store.account_number, store.account_name = "058", ACCT, "Typed Name"
    db.session.commit()
    res = client.get("/api/withdrawal/bank-accounts", headers=auth_headers(vendor))
    acc = res.get_json()["accounts"][0]
    assert acc["account_name"] is None and acc["name_status"] == "pending"
    assert acc["account_number_masked"] == "******0986"


def test_resolve_endpoint_tries_daya_then_paystack(client, lookups):
    lookups["paystack"] = "ADA OBI"
    res = client.post("/api/withdrawal/banks/resolve", json={"bank_code": "50515", "account_number": ACCT})
    assert res.status_code == 200 and res.get_json()["account_name"] == "ADA OBI"
    lookups["paystack"] = "reject"
    assert client.post("/api/withdrawal/banks/resolve",
                       json={"bank_code": "50515", "account_number": ACCT}).status_code == 400
    lookups["paystack"] = "fail"
    assert client.post("/api/withdrawal/banks/resolve",
                       json={"bank_code": "50515", "account_number": ACCT}).status_code == 503


# ── Payouts wait for a confirmed name ─────────────────────────────────────────

def _pending_account(vendor, name=None):
    acc = VendorBankAccount(vendor_id=vendor.id, bank_name=BANK, bank_code="50515", account_number=ACCT,
                            account_name=name, is_verified=False, is_default=True,
                            recipient_code=f"RCP_old_{vendor.id}")
    db.session.add(acc)
    db.session.commit()
    return acc


def test_withdrawal_held_when_name_unconfirmed(client, make_vendor, auth_headers, lookups, monkeypatch):
    monkeypatch.setenv("ACTIVE_ESCROW_PROVIDER", "paystack")
    vendor, _ = make_vendor()
    _pending_account(vendor, name=BANK)
    _credit_vendor_ledger(vendor.id, 5000, "SALE-1", "Sale")
    db.session.commit()
    sent = []
    monkeypatch.setattr(paystack_provider, "paystack_transfer_to_vendor", lambda **k: sent.append(k))
    res = client.post("/api/withdrawal/withdrawals", json={}, headers=auth_headers(vendor))
    assert res.status_code == 400
    assert res.get_json()["code"] == "ACCOUNT_NAME_NOT_CONFIRMED"
    assert res.get_json()["message"] == svc.HOLD_NOTICE
    assert sent == [] and _get_ledger_balance(vendor.id) == Decimal("5000")
    assert Notification.query.filter_by(user_id=vendor.id, title=svc.HOLD_TITLE).count() == 1


def test_withdrawal_goes_ahead_after_one_successful_lookup(client, make_vendor, auth_headers, lookups,
                                                           recipients, monkeypatch):
    monkeypatch.setenv("ACTIVE_ESCROW_PROVIDER", "paystack")
    vendor, _ = make_vendor()
    _pending_account(vendor)
    _credit_vendor_ledger(vendor.id, 5000, "SALE-1", "Sale")
    db.session.commit()
    lookups["paystack"] = "OKEREKE INNOCENT C."
    monkeypatch.setattr(paystack_provider, "paystack_transfer_to_vendor",
                        lambda **k: {"success": True, "transfer_code": "TRF_1"})
    res = client.post("/api/withdrawal/withdrawals", json={}, headers=auth_headers(vendor))
    assert res.status_code == 200, res.get_json()
    assert _get_ledger_balance(vendor.id) == Decimal("0")


def _escrow_order(make_vendor, make_order, method):
    vendor, _ = make_vendor()
    order = make_order(vendor, total=10000, payment_method=method)
    escrow = EscrowTransaction(order_id=order.id, transaction_number=f"T-{order.id}",
                               status=EscrowStatus.IN_ESCROW, amount=10000, fee_amount=300)
    db.session.add(escrow)
    db.session.commit()
    return vendor, order, escrow


def test_paystack_payout_held_when_name_unconfirmed(make_vendor, make_order, lookups, monkeypatch):
    vendor, order, escrow = _escrow_order(make_vendor, make_order, "PAYSTACK")
    _pending_account(vendor, name="Vendor")
    sent = []
    monkeypatch.setattr(paystack_provider, "paystack_transfer_to_vendor", lambda **k: sent.append(k))
    assert escrow_routes._paystack_payout_vendor(order, escrow) is False
    db.session.commit()
    assert sent == []
    notes = Notification.query.filter_by(user_id=vendor.id, title=svc.HOLD_TITLE).all()
    assert len(notes) == 1 and svc.HOLD_NOTICE in notes[0].message
    # Second attempt for the same order: no second notice.
    escrow_routes._paystack_payout_vendor(order, escrow)
    db.session.commit()
    assert Notification.query.filter_by(user_id=vendor.id, title=svc.HOLD_TITLE).count() == 1


def test_daya_payout_never_sends_store_or_fake_name(make_vendor, make_order, lookups, monkeypatch):
    vendor, order, escrow = _escrow_order(make_vendor, make_order, "CRYPTO")
    monkeypatch.setattr(daya_service, "get_rate", lambda **k: {"rate": 1500})
    monkeypatch.setattr(daya_service, "get_merchant_balance",
                        lambda: {"data": {"collection_balance_usd": 0, "withdrawal_balance_usd": 1000}})
    sent = []
    monkeypatch.setattr(daya_service, "transfer_ngn_to_vendor", lambda **k: sent.append(k) or {"success": True})
    # Only storefront bank details (the old code sent the store name): held.
    store = vendor.storefront
    store.bank_code, store.account_number = "50515", ACCT
    db.session.commit()
    assert payments_routes._payout_vendor_via_daya(order, escrow) is False
    assert sent == []
    # The bank confirms the name: the confirmed name is what Daya gets.
    lookups["daya"] = "OKEREKE INNOCENT C."
    assert payments_routes._payout_vendor_via_daya(order, escrow) is True
    assert sent[0]["account_name"] == "OKEREKE INNOCENT C."


# ── Boot flag and hourly retry ────────────────────────────────────────────────

def test_boot_flags_bank_name_rows_without_network(make_vendor, monkeypatch):
    vendor, _ = make_vendor()
    acc = VendorBankAccount(vendor_id=vendor.id, bank_name=BANK, bank_code="50515", account_number=ACCT,
                            account_name=BANK, is_verified=True, is_default=True)
    good = VendorBankAccount(vendor_id=vendor.id, bank_name="GTBank", bank_code="058",
                             account_number="0123456789", account_name="ADA OBI", is_verified=True)
    db.session.add_all([acc, good])
    db.session.commit()

    def boom(*a, **k):
        raise AssertionError("no network at boot")
    monkeypatch.setattr(bank_resolve, "resolve_account", boom)
    monkeypatch.setattr(daya_service, "resolve_bank_account", boom)
    assert svc.flag_bad_names() == 1
    assert acc.account_name is None and acc.is_verified is False
    assert good.account_name == "ADA OBI" and good.is_verified is True
    assert svc.flag_bad_names() == 0  # idempotent


def test_retry_job_fixes_and_notifies_once(make_vendor, lookups, recipients):
    vendor, _ = make_vendor()
    acc = _pending_account(vendor, name=BANK)
    assert svc.run_name_retry() == {"checked": 1, "fixed": 0}
    assert acc.account_name is None
    lookups["paystack"] = "OKEREKE INNOCENT C."
    assert svc.run_name_retry() == {"checked": 1, "fixed": 1}
    assert acc.account_name == "OKEREKE INNOCENT C." and acc.is_verified is True
    assert recipients == ["OKEREKE INNOCENT C."]
    assert svc.run_name_retry()["checked"] == 0
    assert Notification.query.filter_by(user_id=vendor.id, title=svc.CONFIRMED_TITLE).count() == 1


def test_seller_check_again(client, make_vendor, auth_headers, lookups, recipients):
    vendor, _ = make_vendor()
    acc = _pending_account(vendor)
    res = client.post(f"/api/withdrawal/bank-accounts/{acc.id}/recheck", headers=auth_headers(vendor))
    assert res.status_code == 200 and res.get_json()["confirmed"] is False
    lookups["daya"] = "OKEREKE INNOCENT C."
    res = client.post(f"/api/withdrawal/bank-accounts/{acc.id}/recheck", headers=auth_headers(vendor))
    assert res.get_json()["confirmed"] is True
    assert res.get_json()["account"]["account_name"] == "OKEREKE INNOCENT C."
    other, _ = make_vendor()
    assert client.post(f"/api/withdrawal/bank-accounts/{acc.id}/recheck",
                       headers=auth_headers(other)).status_code == 404


# ── Settings ignore a typed name ──────────────────────────────────────────────

def test_settings_ignore_typed_account_name(client, make_vendor, auth_headers, lookups):
    vendor, store = make_vendor()
    h = auth_headers(vendor)
    res = client.patch("/api/vendor/update-settings", headers=h, json={"account_name": "Ada Fabrics"})
    assert res.status_code == 200
    db.session.refresh(store)
    assert store.account_name is None
    lookups["paystack"] = "ADA OBI"
    client.patch("/api/vendor/update-settings", headers=h,
                 json={"bank_code": "058", "account_number": "0123456789", "account_name": "Typed"})
    db.session.refresh(store)
    assert store.account_name == "ADA OBI"


# ── Admin list ────────────────────────────────────────────────────────────────

@pytest.fixture
def admin_headers(monkeypatch):
    monkeypatch.setenv("ADMIN_2FA_REQUIRED", "false")
    admin = AdminUser(name="Ops", email=f"ops-{datetime.now().timestamp()}@siiqo.test", role="SUPERADMIN")
    admin.set_password("not-a-real-password")
    db.session.add(admin)
    db.session.commit()
    return {"Authorization": f"Bearer {create_access_token(identity=f'admin:{admin.id}')}"}


def test_admin_list_and_recheck(client, make_vendor, auth_headers, admin_headers, lookups, recipients):
    v1, s1 = make_vendor()
    pending = _pending_account(v1, name=BANK)
    v2, s2 = make_vendor()
    mismatch = VendorBankAccount(vendor_id=v2.id, bank_name="GTBank", bank_code="058",
                                 account_number="0123456789", account_name="CHUKWU EMEKA", is_verified=True)
    v3, s3 = make_vendor()
    fine = VendorBankAccount(vendor_id=v3.id, bank_name="GTBank", bank_code="058",
                             account_number="0123456780", account_name="TEST USER", is_verified=True)
    db.session.add_all([mismatch, fine])
    db.session.commit()

    assert client.get("/api/admin/payout-accounts", headers=auth_headers(v1)).status_code == 403
    res = client.get("/api/admin/payout-accounts", headers=admin_headers)
    assert res.status_code == 200
    body = res.get_json()
    by_id = {r["id"]: r for r in body["accounts"]}
    assert set(by_id) == {pending.id, mismatch.id}
    assert by_id[pending.id]["status"] == "pending" and "name_pending" in by_id[pending.id]["reasons"]
    assert by_id[pending.id]["account_number_masked"] == "******0986"
    assert by_id[mismatch.id]["name_mismatch_warning"] is True and by_id[mismatch.id]["status"] == "verified"
    assert ACCT not in res.get_data(as_text=True) and "0123456789" not in res.get_data(as_text=True)

    lookups["paystack"] = "TEST USER"
    res = client.post(f"/api/admin/payout-accounts/{pending.id}/recheck", headers=admin_headers)
    assert res.status_code == 200 and res.get_json()["confirmed"] is True
    assert res.get_json()["account"]["account_name"] == "TEST USER"


def test_name_rules():
    assert svc.is_suspect_name("Moniepoint Microfinance Bank")
    assert svc.is_suspect_name("GTBank") and svc.is_suspect_name("Vendor") and svc.is_suspect_name("")
    assert svc.is_suspect_name("Ada Fabrics", store_names=["Ada Fabrics"])
    assert not svc.is_suspect_name("OKEREKE INNOCENT C.")
    assert not svc.is_suspect_name("BANKOLE ADEWALE")
    assert svc.shares_word_with("OKEREKE INNOCENT C.", "Innocent Okereke")
    assert not svc.shares_word_with("CHUKWU EMEKA", "Test User")
    assert svc.mask_number(ACCT) == "******0986"
