"""Phase T (T-3): Telegram Mini App initData sign-in, POST /api/auth/telegram-webapp.

initData is built here with the algorithm from https://core.telegram.org/bots/webapps
("Validating data received via the Mini App"). No call reaches Telegram.
"""
import hashlib
import hmac
import json
import time
from urllib.parse import urlencode

import pytest

from app.extensions import db
from app.models.user import User, UserRole
from app.services import telegram_webapp as tgw

BOT_TOKEN = "123456:test-bot-token"
TG_SECRET = "test-tg-auth-secret"
URL = "/api/auth/telegram-webapp"


@pytest.fixture(autouse=True)
def _telegram_env(monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", BOT_TOKEN)
    monkeypatch.setenv("TG_AUTH_SECRET", TG_SECRET)
    monkeypatch.delenv("TG_WEBAPP_MAX_AGE_SECONDS", raising=False)


def make_init_data(tg_id=700001, auth_date=None, bot_token=BOT_TOKEN, drop=(), extra=None, **user_extra):
    user = {"id": tg_id, "first_name": "Ada", "last_name": "Obi", "username": "adaobi",
            "language_code": "en", **user_extra}
    fields = {
        "query_id": "AAHdF6IQAAAAAN0XohDhrOrc",
        "user": json.dumps(user, separators=(",", ":")),
        "auth_date": str(int(time.time()) if auth_date is None else auth_date),
        "signature": "c2lnbmF0dXJlLXBsYWNlaG9sZGVy",
        **(extra or {}),
    }
    for key in drop:
        fields.pop(key, None)
    check = "\n".join(f"{k}={fields[k]}" for k in sorted(fields))
    secret = hmac.new(b"WebAppData", bot_token.encode(), hashlib.sha256).digest()
    fields["hash"] = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
    return urlencode(fields)


def _virtual_user(tg_id, set_telegram_id=True):
    """A bot-created account: tg_<id>@siiqo.app, password tg_<id>_<TG_AUTH_SECRET>."""
    user = User(email=f"tg_{tg_id}@siiqo.app", first_name="Bot", last_name="User",
                role=UserRole.BUYER, is_verified=True, is_active=True,
                telegram_id=str(tg_id) if set_telegram_id else None)
    user.set_password(f"tg_{tg_id}_{TG_SECRET}")
    db.session.add(user)
    db.session.commit()
    return user


# ── Verification ────────────────────────────────────────────────────────────

def test_matches_hand_computed_reference():
    """Independent of make_init_data: build the data-check-string by hand."""
    fields = {"auth_date": "1700000000", "query_id": "Q", "user": '{"id":1}'}
    check = "auth_date=1700000000\nquery_id=Q\nuser={\"id\":1}"
    key = hmac.new(b"WebAppData", BOT_TOKEN.encode(), hashlib.sha256).digest()
    assert tgw.compute_hash(fields, BOT_TOKEN) == hmac.new(key, check.encode(), hashlib.sha256).hexdigest()
    # The Login Widget scheme (SHA256(token) as key) must NOT validate Mini App data.
    widget_key = hashlib.sha256(BOT_TOKEN.encode()).digest()
    assert tgw.compute_hash(fields, BOT_TOKEN) != hmac.new(widget_key, check.encode(), hashlib.sha256).hexdigest()


def test_valid_init_data_for_existing_account_logs_in(client, make_user):
    user = make_user()
    user.telegram_id = "700001"
    db.session.commit()
    res = client.post(URL, json={"init_data": make_init_data(700001)})
    assert res.status_code == 200
    body = res.get_json()
    assert body["status"] == "logged_in"
    assert body["access_token"] and body["refresh_token"]
    assert body["user"]["id"] == user.id


def test_bad_hash_refused(client, make_user):
    make_user().telegram_id = "700001"
    db.session.commit()
    init = make_init_data(700001, bot_token="999:other-bot")
    res = client.post(URL, json={"init_data": init})
    assert res.status_code == 401
    assert res.get_json()["code"] == "bad_hash"


def test_tampered_user_refused(client):
    init = make_init_data(700001).replace("700001", "700002")
    res = client.post(URL, json={"init_data": init})
    assert res.status_code == 401
    assert res.get_json()["code"] == "bad_hash"


def test_expired_init_data_refused(client):
    res = client.post(URL, json={"init_data": make_init_data(auth_date=int(time.time()) - 3600)})
    assert res.status_code == 401
    assert res.get_json()["code"] == "expired"


def test_future_auth_date_refused(client):
    res = client.post(URL, json={"init_data": make_init_data(auth_date=int(time.time()) + 3600)})
    assert res.status_code == 401
    assert res.get_json()["code"] == "expired"


def test_window_is_configurable(client, monkeypatch):
    monkeypatch.setenv("TG_WEBAPP_MAX_AGE_SECONDS", "7200")
    res = client.post(URL, json={"init_data": make_init_data(auth_date=int(time.time()) - 3600)})
    assert res.status_code == 200


def test_replayed_init_data_refused(client, make_user):
    make_user().telegram_id = "700001"
    db.session.commit()
    init = make_init_data(700001)
    assert client.post(URL, json={"init_data": init}).status_code == 200
    res = client.post(URL, json={"init_data": init})
    assert res.status_code == 401
    assert res.get_json()["code"] == "replayed"


def test_bad_hash_does_not_burn_the_real_init_data(client):
    good = make_init_data(700001)
    bad = good[:-4] + "0000"
    assert client.post(URL, json={"init_data": bad}).status_code == 401
    assert client.post(URL, json={"init_data": good}).status_code == 200


@pytest.mark.parametrize("missing", ["hash", "auth_date", "user"])
def test_missing_fields_refused(client, missing):
    init = make_init_data(drop=() if missing == "hash" else (missing,))
    if missing == "hash":
        init = "&".join(p for p in init.split("&") if not p.startswith("hash="))
    res = client.post(URL, json={"init_data": init})
    assert res.status_code == 400
    assert res.get_json()["code"] == "missing_field"


def test_missing_init_data_refused(client):
    res = client.post(URL, json={})
    assert res.status_code == 400
    assert res.get_json()["code"] == "missing_init_data"


def test_user_without_id_refused(client):
    init = make_init_data(extra={"user": json.dumps({"first_name": "x"})})
    res = client.post(URL, json={"init_data": init})
    assert res.status_code == 400
    assert res.get_json()["code"] == "missing_field"


def test_repeated_field_refused(client):
    init = make_init_data() + "&auth_date=1"
    res = client.post(URL, json={"init_data": init})
    assert res.status_code == 400


def test_not_configured_without_bot_token(client, monkeypatch):
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    res = client.post(URL, json={"init_data": make_init_data()})
    assert res.status_code == 503


# ── Linking and accounts ────────────────────────────────────────────────────

def test_links_to_existing_telegram_id_without_changing_profile(client, make_user):
    user = make_user()
    user.telegram_id = "700001"
    user.first_name = "Chosen"
    db.session.commit()
    before = User.query.count()
    res = client.post(URL, json={"init_data": make_init_data(700001, first_name="Other")})
    assert res.status_code == 200
    assert res.get_json()["user"]["id"] == user.id
    assert User.query.count() == before
    assert db.session.get(User, user.id).first_name == "Chosen"


def test_unknown_user_gets_link_token_and_no_account(client):
    res = client.post(URL, json={"init_data": make_init_data(700009)})
    body = res.get_json()
    assert res.status_code == 200
    assert body["status"] == "new_user"
    assert body["link_token"]
    assert "access_token" not in body
    assert User.query.count() == 0


def test_create_makes_one_virtual_account_and_never_a_second(client):
    token = client.post(URL, json={"init_data": make_init_data(700009)}).get_json()["link_token"]
    first = client.post(f"{URL}/create", json={"link_token": token})
    assert first.status_code == 200
    second = client.post(f"{URL}/create", json={"link_token": token})
    assert second.status_code == 200
    assert first.get_json()["user"]["id"] == second.get_json()["user"]["id"]
    users = User.query.filter_by(telegram_id="700009").all()
    assert len(users) == 1
    assert users[0].email == "tg_700009@siiqo.app"
    assert users[0].check_password(f"tg_700009_{TG_SECRET}")  # same scheme as the bot
    # Next Mini App open logs straight in to that account.
    again = client.post(URL, json={"init_data": make_init_data(700009, extra={"query_id": "Q2"})})
    assert again.get_json()["user"]["id"] == users[0].id
    assert User.query.count() == 1


def test_create_refused_without_tg_auth_secret(client, monkeypatch):
    token = client.post(URL, json={"init_data": make_init_data(700009)}).get_json()["link_token"]
    monkeypatch.delenv("TG_AUTH_SECRET")
    res = client.post(f"{URL}/create", json={"link_token": token})
    assert res.status_code == 503
    assert User.query.count() == 0


def test_forged_link_token_refused(client):
    res = client.post(f"{URL}/create", json={"link_token": "eyJ0aWQiOiIxIn0.forged.sig"})
    assert res.status_code == 401
    assert User.query.count() == 0


def test_bot_virtual_account_by_telegram_id_still_logs_in(client):
    bot_user = _virtual_user(700020)
    res = client.post(URL, json={"init_data": make_init_data(700020)})
    assert res.status_code == 200
    assert res.get_json()["user"]["id"] == bot_user.id
    # The bot's own password login keeps working.
    res = client.post("/api/auth/login", json={"email": "tg_700020@siiqo.app",
                                               "password": f"tg_700020_{TG_SECRET}"})
    assert res.status_code == 200
    assert User.query.count() == 1


def test_legacy_virtual_account_without_telegram_id_is_linked_not_duplicated(client):
    legacy = _virtual_user(700030, set_telegram_id=False)
    res = client.post(URL, json={"init_data": make_init_data(700030)})
    assert res.status_code == 200
    assert res.get_json()["user"]["id"] == legacy.id
    assert db.session.get(User, legacy.id).telegram_id == "700030"
    assert User.query.count() == 1


def test_suspended_account_refused(client, make_user):
    user = make_user()
    user.telegram_id = "700001"
    user.is_active = False
    db.session.commit()
    res = client.post(URL, json={"init_data": make_init_data(700001)})
    assert res.status_code == 403


def test_email_link_with_otp(client, make_user, monkeypatch):
    sent = {}
    monkeypatch.setattr("app.routes.auth.send_siiqo_email", lambda **kw: sent.update(kw))
    user = make_user(email="seller@example.com")
    user.is_verified = True
    db.session.commit()
    token = client.post(URL, json={"init_data": make_init_data(700040)}).get_json()["link_token"]

    res = client.post(f"{URL}/link/send-otp", json={"link_token": token, "email": "seller@example.com"})
    assert res.status_code == 200
    otp = sent["otp"]

    wrong = client.post(f"{URL}/link/verify", json={"link_token": token, "email": "seller@example.com",
                                                    "otp": "000000" if otp != "000000" else "111111"})
    assert wrong.status_code == 400
    ok = client.post(f"{URL}/link/verify", json={"link_token": token, "email": "seller@example.com", "otp": otp})
    assert ok.status_code == 200
    assert ok.get_json()["user"]["id"] == user.id
    assert db.session.get(User, user.id).telegram_id == "700040"
    assert User.query.count() == 1


def test_email_link_does_not_reveal_unknown_email(client, monkeypatch):
    monkeypatch.setattr("app.routes.auth.send_siiqo_email", lambda **kw: None)
    token = client.post(URL, json={"init_data": make_init_data(700050)}).get_json()["link_token"]
    res = client.post(f"{URL}/link/send-otp", json={"link_token": token, "email": "nobody@example.com"})
    assert res.status_code == 200


def test_email_link_refuses_account_linked_to_other_telegram(client, make_user, monkeypatch):
    sent = {}
    monkeypatch.setattr("app.routes.auth.send_siiqo_email", lambda **kw: sent.update(kw))
    other = make_user(email="taken@example.com")
    other.telegram_id = "999999"
    db.session.commit()
    token = client.post(URL, json={"init_data": make_init_data(700060)}).get_json()["link_token"]
    res = client.post(f"{URL}/link/send-otp", json={"link_token": token, "email": "taken@example.com"})
    assert res.status_code == 200
    assert not sent  # no code sent
    assert db.session.get(User, other.id).telegram_id == "999999"
