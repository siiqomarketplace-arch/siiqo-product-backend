"""POST /api/withdrawal/banks/resolve keeps account numbers out of URLs."""
import logging

from app.routes import withdrawal as wd

ACCT = "0123456789"


class _Resp:
    status_code = 200

    def json(self):
        return {"status": True, "data": {"account_name": "ADA OBI", "account_number": ACCT}}


def _fake_get(calls):
    def _get(url, headers=None, params=None, timeout=None):
        calls.append(params)
        return _Resp()
    return _get


def test_post_resolves_from_json_body(client, monkeypatch):
    monkeypatch.setenv("PAYSTACK_SECRET_KEY", "sk_test_dummy")
    calls = []
    monkeypatch.setattr(wd.requests, "get", _fake_get(calls))
    res = client.post("/api/withdrawal/banks/resolve", json={"account_number": ACCT, "bank_code": "058"})
    assert res.status_code == 200
    assert res.get_json() == {"status": "success", "account_name": "ADA OBI", "account_number": ACCT}
    assert calls == [{"account_number": ACCT, "bank_code": "058"}]


def test_post_validates_body(client):
    assert client.post("/api/withdrawal/banks/resolve", json={"bank_code": "058"}).status_code == 400
    assert client.post("/api/withdrawal/banks/resolve",
                       json={"account_number": "123", "bank_code": "058"}).status_code == 400


def test_get_still_works_and_logs_without_number(client, monkeypatch, caplog):
    monkeypatch.setenv("PAYSTACK_SECRET_KEY", "sk_test_dummy")
    monkeypatch.setattr(wd.requests, "get", _fake_get([]))
    with caplog.at_level(logging.INFO):
        res = client.get("/api/withdrawal/banks/resolve",
                         query_string={"account_number": ACCT, "bank_code": "058"})
    assert res.status_code == 200 and res.get_json()["account_name"] == "ADA OBI"
    assert "Deprecated GET" in caplog.text
    assert ACCT not in caplog.text


def test_check_accepts_post_body_like_get(client):
    get = client.get("/api/check", query_string={"q": "08031234567"})
    post = client.post("/api/check", json={"q": "08031234567"})
    assert post.status_code == get.status_code
    assert post.get_json()["query_type"] == get.get_json()["query_type"]
    assert client.post("/api/check", json={}).status_code == 400


def test_check_account_accepts_post_body(client):
    res = client.post("/api/check/account", json={"bank_code": "058", "account_number": ACCT})
    assert res.status_code == 200 and res.get_json()["query_type"] == "account"
    assert client.post("/api/check/account", json={"bank_code": "058"}).status_code == 400
