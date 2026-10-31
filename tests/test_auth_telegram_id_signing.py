"""
telegram_id on /api/auth/register, /login, /verify-email, /send-link-otp and
/verify-link-otp is accepted only from a request signed by SiiqoBot with
BOT_INTERNAL_SECRET (timestamp + replay window). Unsigned requests have it
ignored, so web sign-up and login are unchanged. Existing links are untouched.
"""
import json
import time
import uuid
from datetime import timedelta

import pytest

import app.routes.auth as auth_routes
from app.extensions import db
from app.models.user import User, UserRole
from app.services import bot_auth

SECRET = "s" * 40


@pytest.fixture(autouse=True)
def _clear_registration_throttle():
    """/register is IP-throttled in memory (3 per hour); keep this file from using up other tests' quota."""
    from app.middleware import bot_protection
    bot_protection.registration_attempts.clear()
    yield
    bot_protection.registration_attempts.clear()


@pytest.fixture(autouse=True)
def env(monkeypatch):
    monkeypatch.setenv("BOT_INTERNAL_SECRET", SECRET)
    monkeypatch.setattr(auth_routes, "send_siiqo_email", lambda *a, **k: None)
    return monkeypatch


def post(client, path, body, *, signed=False, secret=SECRET, ts=None, nonce=None):
    raw = json.dumps(body).encode()
    headers = {"Content-Type": "application/json"}
    if signed:
        headers[bot_auth.SIGNATURE_HEADER] = bot_auth.sign(
            secret, ts or int(time.time()), nonce or uuid.uuid4().hex, "POST", path, raw)
    return client.post(path, data=raw, headers=headers)


def user(email="ada@example.com", password="a-long-password", telegram_id=None, verified=True):
    u = User(email=email, first_name="Ada", last_name="Obi", role=UserRole.BUYER,
             is_verified=verified, is_active=True, telegram_id=telegram_id)
    u.set_password(password)
    db.session.add(u)
    db.session.commit()
    return u


def tid_of(email):
    db.session.expire_all()
    return User.query.filter_by(email=email).first().telegram_id


# ── login ───────────────────────────────────────────────────────────────────
def test_unsigned_login_ignores_telegram_id(client):
    user()
    res = post(client, "/api/auth/login", {"email": "ada@example.com", "password": "a-long-password",
                                           "telegram_id": "777001"})
    assert res.status_code == 200
    assert tid_of("ada@example.com") is None


def test_unsigned_login_cannot_steal_an_existing_link(client):
    user(telegram_id="777002")
    user(email="mallory@example.com")
    res = post(client, "/api/auth/login", {"email": "mallory@example.com", "password": "a-long-password",
                                           "telegram_id": "777002"})
    assert res.status_code == 200                      # logs in, link ignored (no 409 oracle)
    assert tid_of("mallory@example.com") is None
    assert tid_of("ada@example.com") == "777002"


def test_signed_login_links_telegram_id(client):
    user()
    res = post(client, "/api/auth/login", {"email": "ada@example.com", "password": "a-long-password",
                                           "telegram_id": "777003"}, signed=True)
    assert res.status_code == 200
    assert tid_of("ada@example.com") == "777003"


def test_replayed_signed_login_refused(client):
    user()
    body = {"email": "ada@example.com", "password": "a-long-password", "telegram_id": "777004"}
    ts, nonce = int(time.time()), uuid.uuid4().hex
    assert post(client, "/api/auth/login", body, signed=True, ts=ts, nonce=nonce).status_code == 200
    assert post(client, "/api/auth/login", body, signed=True, ts=ts, nonce=nonce).status_code == 409


@pytest.mark.parametrize("kind", ["wrong_secret", "stale"])
def test_bad_signature_refused(client, kind):
    user()
    body = {"email": "ada@example.com", "password": "a-long-password", "telegram_id": "777005"}
    kw = {"secret": "z" * 40} if kind == "wrong_secret" else {"ts": int(time.time()) - 3600}
    res = post(client, "/api/auth/login", body, signed=True, **kw)
    assert res.status_code == 401
    assert tid_of("ada@example.com") is None


def test_secret_unset_fails_closed(client, env):
    env.delenv("BOT_INTERNAL_SECRET")
    user()
    res = post(client, "/api/auth/login", {"email": "ada@example.com", "password": "a-long-password",
                                           "telegram_id": "777006"}, signed=True)
    assert res.status_code == 200
    assert tid_of("ada@example.com") is None


def test_non_numeric_telegram_id_ignored_even_when_signed(client):
    user()
    post(client, "/api/auth/login", {"email": "ada@example.com", "password": "a-long-password",
                                     "telegram_id": "2348012345678x"}, signed=True)
    assert tid_of("ada@example.com") is None


# ── existing users / virtual accounts ───────────────────────────────────────
def test_existing_link_kept_and_plain_login_unchanged(client):
    user(telegram_id="777007")
    res = post(client, "/api/auth/login", {"email": "ada@example.com", "password": "a-long-password"})
    assert res.status_code == 200 and res.get_json()["access_token"]
    assert tid_of("ada@example.com") == "777007"


def test_virtual_account_login_unchanged(client):
    user(email="tg_777008@siiqo.app", password="tg_777008_secretvalue", telegram_id="777008")
    res = post(client, "/api/auth/login", {"email": "tg_777008@siiqo.app", "password": "tg_777008_secretvalue"})
    assert res.status_code == 200
    assert tid_of("tg_777008@siiqo.app") == "777008"


# ── register / verify-email ─────────────────────────────────────────────────
def test_unsigned_register_ignores_telegram_id(client):
    res = post(client, "/api/auth/register", {"email": "new@example.com", "password": "a-long-password",
                                              "first_name": "N", "last_name": "U", "telegram_id": "777009"})
    assert res.status_code == 201, res.get_data(as_text=True)
    assert tid_of("new@example.com") is None


def test_signed_register_sets_telegram_id(client):
    res = post(client, "/api/auth/register", {"email": "new2@example.com", "password": "a-long-password",
                                              "first_name": "N", "last_name": "U", "telegram_id": "777010"},
               signed=True)
    assert res.status_code == 201, res.get_data(as_text=True)
    assert tid_of("new2@example.com") == "777010"


def _with_otp(u, otp="123456"):
    u.reset_otp = otp
    u.otp_expiry = auth_routes._utcnow() + timedelta(minutes=10)
    db.session.commit()


def test_verify_email_unsigned_ignores_signed_links(client):
    u = user(email="v@example.com", verified=False)
    _with_otp(u)
    res = post(client, "/api/auth/verify-email", {"email": "v@example.com", "otp": "123456", "telegram_id": "777011"})
    assert res.status_code == 200
    assert tid_of("v@example.com") is None
    u2 = user(email="v2@example.com", verified=False)
    _with_otp(u2)
    res = post(client, "/api/auth/verify-email", {"email": "v2@example.com", "otp": "123456",
                                                  "telegram_id": "777012"}, signed=True)
    assert res.status_code == 200
    assert tid_of("v2@example.com") == "777012"


# ── link OTP (bot-only endpoints) ───────────────────────────────────────────
def test_link_otp_requires_signed_telegram_id(client):
    user(email="link@example.com")
    unsigned = post(client, "/api/auth/send-link-otp", {"email": "link@example.com", "telegram_id": "777013"})
    assert unsigned.status_code == 400
    signed = post(client, "/api/auth/send-link-otp", {"email": "link@example.com", "telegram_id": "777013"},
                  signed=True)
    assert signed.status_code == 200
    db.session.expire_all()
    otp = User.query.filter_by(email="link@example.com").first().reset_otp
    bad = post(client, "/api/auth/verify-link-otp", {"email": "link@example.com", "otp": otp, "telegram_id": "777013"})
    assert bad.status_code == 400 and tid_of("link@example.com") is None
    ok = post(client, "/api/auth/verify-link-otp", {"email": "link@example.com", "otp": otp,
                                                     "telegram_id": "777013"}, signed=True)
    assert ok.status_code == 200
    assert tid_of("link@example.com") == "777013"


# ── Telegram Login Widget link_token (website flow) ─────────────────────────
import hashlib  # noqa: E402
import hmac  # noqa: E402

BOT_TOKEN = "123:widget-test-token"


def widget_login(client, env, tg_id):
    env.setenv("TELEGRAM_BOT_TOKEN", BOT_TOKEN)
    data = {"id": str(tg_id), "first_name": "Tolu", "auth_date": str(int(time.time()))}
    check = "\n".join(f"{k}={v}" for k, v in sorted(data.items()))
    key = hashlib.sha256(BOT_TOKEN.encode()).digest()
    data["hash"] = hmac.new(key, check.encode(), hashlib.sha256).hexdigest()
    return client.post("/api/auth/telegram-login", json=data)


def test_widget_new_user_gets_link_token(client, env):
    body = widget_login(client, env, 880001).get_json()
    assert body["status"] == "new_user"
    assert auth_routes.read_widget_link_token(body["link_token"]) == "880001"
    assert body["link_token_expires_in"] == 600


def test_widget_link_otp_flow_with_link_token(client, env):
    token = widget_login(client, env, 880002).get_json()["link_token"]
    user(email="web@example.com")
    sent = post(client, "/api/auth/send-link-otp",
                {"email": "web@example.com", "telegram_id": "880002", "link_token": token})
    assert sent.status_code == 200
    db.session.expire_all()
    otp = User.query.filter_by(email="web@example.com").first().reset_otp
    ok = post(client, "/api/auth/verify-link-otp",
              {"email": "web@example.com", "otp": otp, "telegram_id": "880002", "link_token": token})
    assert ok.status_code == 200 and ok.get_json()["access_token"]
    assert tid_of("web@example.com") == "880002"


def _forged(tid):
    from itsdangerous import URLSafeTimedSerializer
    return URLSafeTimedSerializer("not-the-server-secret", salt="siiqo.telegram-widget.link.v1").dumps({"tid": tid})


def _mini_app_token(app, tid):
    from itsdangerous import URLSafeTimedSerializer
    return URLSafeTimedSerializer(app.config["SECRET_KEY"], salt="siiqo.telegram-webapp.link.v1").dumps({"tid": tid})


@pytest.mark.parametrize("kind", ["forged", "expired", "mismatched", "other_salt", "garbage"])
def test_bad_link_token_refused_on_link_otp(client, env, app, kind):
    user(email="web@example.com")
    if kind == "forged":
        token = _forged("880003")
    elif kind == "expired":
        token = auth_routes.make_widget_link_token("880003")
        env.setattr(auth_routes, "WIDGET_LINK_TOKEN_MAX_AGE_SECONDS", -1)
    elif kind == "mismatched":
        token = auth_routes.make_widget_link_token("880999")
    elif kind == "other_salt":
        token = _mini_app_token(app, "880003")
    else:
        token = "abc.def"
    res = post(client, "/api/auth/send-link-otp",
               {"email": "web@example.com", "telegram_id": "880003", "link_token": token})
    assert res.status_code == 401 and res.get_json()["code"] == "BAD_LINK_TOKEN"
    db.session.expire_all()
    assert User.query.filter_by(email="web@example.com").first().reset_otp is None


def test_link_otp_without_token_or_signature_refused(client):
    user(email="web@example.com")
    res = post(client, "/api/auth/send-link-otp", {"email": "web@example.com", "telegram_id": "880004"})
    assert res.status_code == 400


def test_register_and_verify_email_with_link_token(client, env):
    token = auth_routes.make_widget_link_token("880005")
    res = post(client, "/api/auth/register", {"email": "s@example.com", "password": "a-long-password",
                                              "first_name": "S", "last_name": "U", "link_token": token})
    assert res.status_code == 201
    assert tid_of("s@example.com") == "880005"

    u = user(email="v3@example.com", verified=False)
    _with_otp(u)
    res = post(client, "/api/auth/verify-email",
               {"email": "v3@example.com", "otp": "123456", "telegram_id": "880006",
                "link_token": auth_routes.make_widget_link_token("880006")})
    assert res.status_code == 200 and tid_of("v3@example.com") == "880006"


@pytest.mark.parametrize("kind", ["forged", "mismatched"])
def test_verify_email_bad_link_token_verifies_without_linking(client, kind):
    u = user(email="v4@example.com", verified=False)
    _with_otp(u)
    token = _forged("880007") if kind == "forged" else auth_routes.make_widget_link_token("880999")
    res = post(client, "/api/auth/verify-email",
               {"email": "v4@example.com", "otp": "123456", "telegram_id": "880007", "link_token": token})
    assert res.status_code == 200
    assert tid_of("v4@example.com") is None


def test_link_token_cannot_steal_existing_link(client):
    user(email="owner@example.com", telegram_id="880008")
    user(email="thief@example.com")
    token = auth_routes.make_widget_link_token("880008")
    res = post(client, "/api/auth/send-link-otp",
               {"email": "thief@example.com", "telegram_id": "880008", "link_token": token})
    assert res.status_code == 409
    assert tid_of("owner@example.com") == "880008"
