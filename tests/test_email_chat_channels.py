"""Every Siiqo email carries the WhatsApp + Telegram chat line (shared brand footer)."""
from pathlib import Path

EMAILS = Path(__file__).resolve().parent.parent / "app" / "templates" / "emails"
WA_LINK = "https://wa.me/2347039600678?text=hi"
TG_LINK = "https://t.me/SiiqoBot"


def test_footer_has_the_chat_line():
    footer = (EMAILS / "_brand_footer.html").read_text(encoding="utf-8")
    assert "Chat with Siiqo on WhatsApp:" in footer
    assert f'href="{WA_LINK}"' in footer and ">wa.me/2347039600678</a>" in footer
    assert f'href="{TG_LINK}"' in footer and ">t.me/SiiqoBot</a>" in footer


def test_every_email_template_includes_the_footer():
    missing = [
        p.name
        for p in sorted(EMAILS.glob("*.html"))
        if not p.name.startswith("_") and "_brand_footer.html" not in p.read_text(encoding="utf-8")
    ]
    assert missing == []


def test_rendered_notification_shows_the_chat_line(app):
    from flask import render_template

    with app.test_request_context():
        html = render_template(
            "emails/base_notification.html",
            first_name="Ada",
            title="Delivered",
            message="Please confirm delivery.",
            cta_text="Open",
            cta_url="https://siiqo.com",
            logo_url="https://siiqo.com/images/Siiqo.png",
        )
    assert "Chat with Siiqo on WhatsApp:" in html
    assert WA_LINK in html and TG_LINK in html
