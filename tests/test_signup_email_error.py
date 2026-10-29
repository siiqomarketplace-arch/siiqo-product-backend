"""C6: when the verification email fails, signup says so plainly and never
returns the exception text (it can contain mail-server details)."""
import app.routes.auth as auth_routes


def test_email_failure_message_hides_exception(client, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("SMTP 535 internal-mail-host credentials rejected")
    monkeypatch.setattr(auth_routes, "send_siiqo_email", boom)

    res = client.post("/api/auth/register", json={
        "email": "new.seller@example.org", "password": "a-long-password-1",
        "first_name": "New", "last_name": "Seller", "phone": "08000000000",
    })
    body = res.get_data(as_text=True)
    assert res.status_code in (201, 500), body
    assert "SMTP" not in body and "internal-mail-host" not in body
