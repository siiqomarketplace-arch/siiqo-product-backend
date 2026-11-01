"""Language foundation (D19): locale resolution, per-key English fallback for
messages, email template fallback, and the preferred_locale setting."""
import pytest

from app import i18n
from app.i18n import messages as messages_mod
from app.utils import email as email_mod


def test_normalize_and_resolve(monkeypatch):
    monkeypatch.delenv("LANGUAGES", raising=False)
    assert i18n.normalize_locale("en-NG") == "en"
    assert i18n.normalize_locale("YO") == "yo"
    assert i18n.normalize_locale("fr") is None
    assert i18n.normalize_locale(None) is None
    assert i18n.enabled_locales() == ("en",)
    # English only: every saved language resolves to English.
    assert i18n.resolve_locale("pcm") == "en"
    assert i18n.resolve_locale(None) == "en"
    monkeypatch.setenv("LANGUAGES", "en, pcm, xx")
    assert i18n.enabled_locales() == ("en", "pcm")
    assert i18n.resolve_locale("pcm") == "pcm"
    assert i18n.resolve_locale("yo") == "en"


def test_message_falls_back_to_english_per_key(monkeypatch):
    monkeypatch.setenv("LANGUAGES", "en,pcm")
    fake = {
        "en": {"a": "Hello {name}", "b": "Bye", "c": "Pay {amount}"},
        "pcm": {"a": "How far {name}", "b": "", "c": "Pay {wrong_placeholder}"},
    }
    monkeypatch.setattr(i18n, "MESSAGES", fake)
    assert i18n.message("a", "pcm", name="Ada") == "How far Ada"
    assert i18n.message("b", "pcm") == "Bye"                     # empty -> English
    assert i18n.message("c", "pcm", amount="N500") == "Pay N500"  # bad placeholder -> English
    assert i18n.message("a", "yo", name="Ada") == "Hello Ada"     # not enabled -> English
    assert i18n.message("a", None, name="Ada") == "Hello Ada"
    with pytest.raises(KeyError):
        i18n.message("missing")


def test_only_english_messages_ship():
    assert set(messages_mod.MESSAGES) == {"en"}


def test_email_template_fallback(monkeypatch):
    monkeypatch.delenv("LANGUAGES", raising=False)
    assert i18n.email_template_names("otp", "yo") == ["emails/otp.html"]
    monkeypatch.setenv("LANGUAGES", "en,yo")
    assert i18n.email_template_names("otp", "yo") == ["emails/yo/otp.html", "emails/otp.html"]
    assert i18n.email_template_names("otp", "en") == ["emails/otp.html"]


def test_send_email_uses_locale_with_english_fallback(app, monkeypatch):
    """No yo/ template exists, so a Yoruba user gets the English template."""
    monkeypatch.setenv("LANGUAGES", "en,yo")
    for var in ("MAIL_SERVER", "MAIL_USERNAME", "MAIL_PASSWORD"):
        monkeypatch.delenv(var, raising=False)
    seen = {}

    def fake_render(names, **context):
        seen["names"], seen["locale"] = names, context.get("locale")
        from flask import render_template as real
        return real(names, **context)

    sent = {}
    monkeypatch.setattr(email_mod, "render_template", fake_render)
    monkeypatch.setattr(email_mod, "_send_email_async", lambda *a: sent.setdefault("html", a[7]))

    class _NoThread:
        def __init__(self, target, args):
            self.target, self.args = target, args
            self.daemon = False

        def start(self):
            self.target(*self.args)

    monkeypatch.setattr(email_mod.threading, "Thread", _NoThread)
    with app.test_request_context():
        assert email_mod.send_siiqo_email("seller@example.com", "Notice", "system_notice", locale="yo",
                                          first_name="Ada", notice_text="Hello there")
    assert seen["names"] == ["emails/yo/system_notice.html", "emails/system_notice.html"]
    assert seen["locale"] == "yo"
    assert "Hello there" in sent["html"]


def test_send_email_defaults_to_english(app, monkeypatch):
    monkeypatch.delenv("LANGUAGES", raising=False)
    seen = {}
    monkeypatch.setattr(email_mod, "render_template", lambda names, **c: seen.update(names=names, locale=c["locale"]) or "")
    monkeypatch.setattr(email_mod.threading, "Thread", lambda target, args: type("T", (), {"daemon": False, "start": lambda self: None})())
    with app.test_request_context():
        email_mod.send_siiqo_email("seller@example.com", "Notice", "system_notice", first_name="Ada")
    assert seen == {"names": ["emails/system_notice.html"], "locale": "en"}


# ── preferred_locale setting ────────────────────────────────────────────────

def test_locale_setting_api(client, auth_headers, make_user, monkeypatch):
    monkeypatch.delenv("LANGUAGES", raising=False)
    user = make_user()
    h = auth_headers(user)
    res = client.get("/api/account/locale", headers=h)
    assert res.status_code == 200
    assert res.json == {"locale": "en", "preferred_locale": None, "enabled": ["en"]}

    res = client.put("/api/account/locale", json={"locale": "yo"}, headers=h)
    assert res.status_code == 400 and res.json["code"] == "LOCALE_NOT_AVAILABLE"
    res = client.put("/api/account/locale", json={"locale": "klingon"}, headers=h)
    assert res.status_code == 400 and res.json["code"] == "UNKNOWN_LOCALE"
    res = client.put("/api/account/locale", json={}, headers=h)
    assert res.status_code == 400

    res = client.put("/api/account/locale", json={"locale": "en-NG"}, headers=h)
    assert res.status_code == 200 and res.json["preferred_locale"] == "en"
    assert user.to_public_dict()["preferred_locale"] == "en"

    monkeypatch.setenv("LANGUAGES", "en,pcm")
    res = client.put("/api/account/locale", json={"locale": "pcm"}, headers=h)
    assert res.json["locale"] == "pcm"
    assert i18n.user_locale(user) == "pcm"
    # Switched off again later: the saved choice stays, but Siiqo writes in English.
    monkeypatch.setenv("LANGUAGES", "en")
    assert i18n.user_locale(user) == "en"

    res = client.put("/api/account/locale", json={"locale": None}, headers=h)
    assert res.json["preferred_locale"] is None


def test_locale_in_privacy_export(client, auth_headers, make_user):
    user = make_user()
    user.preferred_locale = "en"
    from app.extensions import db
    db.session.commit()
    res = client.get("/api/privacy/export", headers=auth_headers(user))
    assert res.json["account"]["preferred_locale"] == "en"
