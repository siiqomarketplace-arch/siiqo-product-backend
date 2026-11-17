"""Log-text masking helpers (stored/sent values are never changed)."""
import logging

from app.utils.log_mask import mask_account, mask_email, mask_emails_in, mask_phone


def test_mask_email():
    assert mask_email("webmaster@gmail.com") == "we***@gmail.com"
    assert mask_email("ab@x.co") == "a***@x.co"
    assert mask_email("a@x.co") == "a***@x.co"
    assert mask_email("not-an-email") == "***"
    assert mask_email(None) == "***"
    assert mask_email(123) == "***"


def test_mask_emails_in_text():
    text = "SMTP refused {'ada.obi@example.com': (550, b'no')} and bob@test.ng"
    out = mask_emails_in(text)
    assert "ada.obi@example.com" not in out and "bob@test.ng" not in out
    assert "ad***@example.com" in out and "bo***@test.ng" in out


def test_mask_phone():
    assert mask_phone("+2348031234567") == "***4567"
    assert mask_phone("0803 123 4567") == "***4567"
    assert mask_phone("123") == "***"
    assert mask_phone(None) == "***"


def test_mask_account():
    assert mask_account("0123456789") == "******6789"
    assert mask_account("12") == "***"
    assert mask_account(None) == "***"


def test_email_sent_log_is_masked(monkeypatch, caplog):
    from app.utils import email as email_mod
    with caplog.at_level(logging.INFO):
        email_mod._send_email_async(None, 587, None, None, "noreply@siiqo.com",
                                    "webmaster@gmail.com", "Hi", "<p>x</p>", {})
    assert "webmaster@gmail.com" not in caplog.text
    assert "we***@gmail.com" in caplog.text
