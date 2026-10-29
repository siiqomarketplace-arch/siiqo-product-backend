"""S9: wrong reset codes are counted in the database (shared by all workers),
the code is invalidated after 5 misses, and a new code starts a fresh count."""
from datetime import datetime, timedelta, timezone

import pytest

from app.extensions import db
from app.models.user import PasswordResetAttempt, User


@pytest.fixture(autouse=True)
def _no_rate_limit(app):
    from app.extensions import limiter
    limiter.enabled = False
    yield
    limiter.enabled = True


def _issue(user, code):
    user.reset_otp = code
    user.otp_expiry = datetime.now(timezone.utc) + timedelta(minutes=10)
    db.session.commit()


def _reset(client, user, code):
    return client.post("/api/auth/reset-password",
                       json={"email": user.email, "otp": code, "password": "a-new-password-1"})


def test_five_wrong_codes_invalidate_the_code(client, make_user):
    user = make_user()
    _issue(user, "123456")
    for _ in range(4):
        assert _reset(client, user, "000000").status_code == 400
    assert PasswordResetAttempt.query.get(user.id).failed_count == 4
    res = _reset(client, user, "000000")
    assert "invalidated" in res.get_json()["message"]
    # the right code no longer works either
    assert _reset(client, user, "123456").status_code == 400
    assert db.session.get(User, user.id).reset_otp is None


def test_new_code_starts_fresh_count(client, make_user):
    user = make_user()
    _issue(user, "123456")
    for _ in range(3):
        _reset(client, user, "000000")
    _issue(user, "654321")
    assert _reset(client, user, "000000").status_code == 400
    assert PasswordResetAttempt.query.get(user.id).failed_count == 1


def test_correct_code_resets_password_and_clears_count(client, make_user):
    user = make_user()
    _issue(user, "123456")
    _reset(client, user, "000000")
    assert _reset(client, user, "123456").status_code == 200
    assert db.session.get(PasswordResetAttempt, user.id) is None
    assert db.session.get(User, user.id).check_password("a-new-password-1")
