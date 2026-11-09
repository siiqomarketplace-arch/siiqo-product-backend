"""
Bot chat features (Telegram + WhatsApp): log in with an email code (no password
in chat), Safe Pay requests from chat (limits per chat user), and the Safe Pay
deadline on the buyer's order.
"""
import json
import time
import uuid

import pytest

from app.extensions import db
from app.models.pay_request import PayRequest
from app.models.user import User
from app.services import bot_auth

SECRET = "x" * 40
BASE = "/api/internal/bot"


@pytest.fixture(autouse=True)
def bot_env(monkeypatch):
    monkeypatch.setenv("BOT_INTERNAL_SECRET", SECRET)
    return monkeypatch


@pytest.fixture
def codes(monkeypatch):
    sent = []
    import app.utils.email as email
    monkeypatch.setattr(email, "send_siiqo_email", lambda **k: sent.append(k) or True)
    return sent


def call(client, path, body, secret=SECRET):
    raw = json.dumps(body).encode()
    headers = {"Content-Type": "application/json"}
    if secret:
        headers[bot_auth.SIGNATURE_HEADER] = bot_auth.sign(secret, int(time.time()), uuid.uuid4().hex, "POST",
                                                          f"{BASE}{path}", raw)
    return client.post(f"{BASE}{path}", data=raw, headers=headers)


# ── Email-code login ───────────────────────────────────────────────────────
def test_login_code_round_trip_links_telegram(client, make_user, codes):
    user = make_user(email="ada@example.com")
    res = call(client, "/login-code/send", {"platform": "telegram", "user_id": "7123456789", "email": "Ada@Example.com"})
    assert res.status_code == 200 and res.get_json()["status"] == "sent"
    code = codes[0]["otp"]
    db.session.expire_all()
    stored = db.session.get(User, user.id).reset_otp
    assert stored != code and len(stored) == 10              # a keyed fingerprint that fits VARCHAR(10)
    bad = call(client, "/login-code/verify", {"platform": "telegram", "user_id": "7123456789",
                                              "email": "ada@example.com", "code": "000000" if code != "000000" else "111111"})
    assert bad.status_code == 400
    ok = call(client, "/login-code/verify", {"platform": "telegram", "user_id": "7123456789",
                                             "email": "ada@example.com", "code": code})
    assert ok.status_code == 200 and ok.get_json()["access_token"]
    db.session.expire_all()
    assert db.session.get(User, user.id).telegram_id == "7123456789"
    # The code works once.
    again = call(client, "/login-code/verify", {"platform": "telegram", "user_id": "7123456789",
                                                "email": "ada@example.com", "code": code})
    assert again.status_code == 400


def test_whatsapp_login_never_sets_telegram_id(client, make_user, codes):
    user = make_user(email="bola@example.com")
    call(client, "/login-code/send", {"platform": "whatsapp", "user_id": "2348031234567", "email": "bola@example.com"})
    ok = call(client, "/login-code/verify", {"platform": "whatsapp", "user_id": "2348031234567",
                                             "email": "bola@example.com", "code": codes[0]["otp"]})
    assert ok.status_code == 200
    db.session.expire_all()
    assert db.session.get(User, user.id).telegram_id is None


def test_unknown_email_gets_the_same_answer_and_no_email(client, codes):
    res = call(client, "/login-code/send", {"platform": "telegram", "user_id": "42", "email": "nobody@example.com"})
    assert res.status_code == 200 and res.get_json()["status"] == "sent"
    assert codes == []


def test_login_code_needs_the_bot_signature(client, make_user, codes):
    make_user(email="ada@example.com")
    res = call(client, "/login-code/send", {"platform": "telegram", "user_id": "42", "email": "ada@example.com"},
               secret="y" * 40)
    assert res.status_code == 401
    assert codes == []


# ── Safe Pay requests from chat ────────────────────────────────────────────
def test_chat_pay_request_counts_per_chat_user(client, monkeypatch):
    from app.services import pay_requests as svc
    monkeypatch.setattr(svc, "MAX_PER_IP_PER_DAY", 1)
    a = call(client, "/pay-requests", {"platform": "telegram", "user_id": "1", "seller": "@seller_one", "item": "Shoes"})
    assert a.status_code == 201 and a.get_json()["request"]["url"].endswith(a.get_json()["request"]["id"])
    # A different chat user is not blocked by the first one's limit (they share the bot's IP).
    b = call(client, "/pay-requests", {"platform": "whatsapp", "user_id": "2348000000001", "seller": "@seller_two"})
    assert b.status_code == 201
    c = call(client, "/pay-requests", {"platform": "telegram", "user_id": "1", "seller": "@seller_three"})
    assert c.status_code == 429
    assert PayRequest.query.count() == 2
