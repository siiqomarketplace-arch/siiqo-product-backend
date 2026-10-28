"""S1: a normal user token must never pass an admin check, even when the
user's numeric id equals an existing AdminUser id."""
import pytest
from flask_jwt_extended import create_access_token

from app.extensions import db
from app.models.admin import AdminUser


@pytest.fixture
def colliding_accounts(make_user):
    """A normal user and a SUPERADMIN that share the same numeric id."""
    user = make_user()
    admin = AdminUser(id=user.id, name="Test Admin", email="admin@example.com", role="SUPERADMIN")
    admin.set_password("not-a-real-password")
    db.session.add(admin)
    db.session.commit()
    return user, admin


def _bearer(identity):
    return {"Authorization": f"Bearer {create_access_token(identity=identity)}"}


def test_ids_really_collide(colliding_accounts):
    user, admin = colliding_accounts
    assert user.id == admin.id


@pytest.mark.parametrize("method,path,body", [
    ("post", "/api/admin/users/{uid}/adjust-balance", {"action": "CREDIT", "amount": 1000}),
    ("get", "/api/admin/daya/balance", None),
    ("post", "/api/grants", {"title": "x"}),
])
def test_user_token_is_refused(client, colliding_accounts, method, path, body):
    user, _ = colliding_accounts
    url = path.format(uid=user.id)

    res = getattr(client, method)(url, json=body, headers=_bearer(str(user.id)))

    assert res.status_code in (401, 403), (url, res.status_code, res.get_json())


@pytest.mark.parametrize("method,path,body", [
    ("post", "/api/admin/users/{uid}/adjust-balance", {}),
    ("get", "/api/admin/daya/balance", None),
    ("post", "/api/grants", {}),
])
def test_admin_token_passes_the_admin_check(client, colliding_accounts, method, path, body):
    user, admin = colliding_accounts
    url = path.format(uid=user.id)

    res = getattr(client, method)(url, json=body, headers=_bearer(f"admin:{admin.id}"))

    # The request may still fail validation or (for Daya) the blocked network,
    # but it must get past the admin check.
    assert res.status_code not in (401, 403), (url, res.status_code, res.get_json())
