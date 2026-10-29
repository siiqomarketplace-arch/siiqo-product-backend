"""S7 (backend side): no built-in TG_AUTH_SECRET, and bot notifications carry
the shared secret header when it is configured."""
import hashlib
import hmac
import time

from app.models import communication


def _signed(bot_token, **fields):
    data = {k: str(v) for k, v in fields.items()}
    check = "\n".join(f"{k}={v}" for k, v in sorted(data.items()))
    key = hashlib.sha256(bot_token.encode()).digest()
    data["hash"] = hmac.new(key, check.encode(), hashlib.sha256).hexdigest()
    return data


def test_telegram_signup_refused_without_secret(client, monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123:test-token")
    monkeypatch.delenv("TG_AUTH_SECRET", raising=False)
    body = _signed("123:test-token", id=555001, first_name="T", auth_date=int(time.time()))
    body["create_new"] = True
    res = client.post("/api/auth/telegram-login", json=body)
    assert res.status_code == 503


def test_notification_sends_secret_header(monkeypatch):
    sent = {}
    monkeypatch.setenv("BOT_ORCHESTRATOR_URL", "https://bot.example")
    monkeypatch.setenv("SIIQO_NOTIFY_SECRET", "notify-test-secret")
    monkeypatch.setattr(communication.requests, "post",
                        lambda url, headers=None, json=None, timeout=None: sent.update(url=url, headers=headers))
    communication._send_telegram_notification_async("42", "t", "m", "ORDER", None)
    assert sent["headers"]["X-Siiqo-Notify-Secret"] == "notify-test-secret"
