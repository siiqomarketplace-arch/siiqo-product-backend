"""Self-serve developer API keys for the Passport API."""
from flask_jwt_extended import create_access_token

from app.extensions import db
from app.models.admin import AdminUser
from app.models.passport import PassportApiClient
from app.models.user import UserRole


def test_create_list_and_use_key(client, make_user, make_vendor, auth_headers):
    dev = make_user(role=UserRole.BUYER)
    _, store = make_vendor()
    res = client.post("/api/passport/keys", json={"name": "My plugin"}, headers=auth_headers(dev))
    assert res.status_code == 201
    created = res.get_json()
    assert created["plan"] == "free" and created["name"] == "My plugin"
    assert created["api_key"].startswith("sqp_") and created["key_prefix"] == created["api_key"][:12]
    assert created["id"] and created["created_at"]

    row = db.session.get(PassportApiClient, created["id"])
    assert row.owner_user_id == dev.id and row.key_hash != created["api_key"]

    listed = client.get("/api/passport/keys", headers=auth_headers(dev))
    assert listed.status_code == 200
    keys = listed.get_json()["keys"]
    assert len(keys) == 1 and keys[0]["key_prefix"] == created["key_prefix"]
    assert created["api_key"] not in listed.get_data(as_text=True)
    assert row.key_hash not in listed.get_data(as_text=True)

    assert client.get(f"/api/passport/{store.store_slug}",
                      headers={"X-Siiqo-Api-Key": created["api_key"]}).status_code == 200


def test_max_three_active_keys(client, make_user, auth_headers):
    dev = make_user()
    ids = []
    for i in range(3):
        res = client.post("/api/passport/keys", json={"name": f"k{i}"}, headers=auth_headers(dev))
        assert res.status_code == 201
        ids.append(res.get_json()["id"])
    assert client.post("/api/passport/keys", json={"name": "k4"}, headers=auth_headers(dev)).status_code == 409
    assert client.delete(f"/api/passport/keys/{ids[0]}", headers=auth_headers(dev)).status_code == 200
    assert client.post("/api/passport/keys", json={"name": "k4"}, headers=auth_headers(dev)).status_code == 201


def test_revoke_owner_only_and_revoked_key_rejected(client, make_user, make_vendor, auth_headers):
    owner, other = make_user(), make_user()
    _, store = make_vendor()
    created = client.post("/api/passport/keys", json={"name": "k"}, headers=auth_headers(owner)).get_json()

    assert client.delete(f"/api/passport/keys/{created['id']}", headers=auth_headers(other)).status_code == 404
    assert db.session.get(PassportApiClient, created["id"]).is_active is True
    assert client.get("/api/passport/keys", headers=auth_headers(other)).get_json()["keys"] == []

    assert client.delete(f"/api/passport/keys/{created['id']}", headers=auth_headers(owner)).status_code == 200
    db.session.expire_all()
    assert db.session.get(PassportApiClient, created["id"]).is_active is False
    listed = client.get("/api/passport/keys", headers=auth_headers(owner)).get_json()["keys"]
    assert listed[0]["is_active"] is False
    assert client.get(f"/api/passport/{store.store_slug}",
                      headers={"X-Siiqo-Api-Key": created["api_key"]}).status_code == 401


def test_requires_sign_in_and_name(client, make_user, auth_headers):
    assert client.post("/api/passport/keys", json={"name": "k"}).status_code == 401
    assert client.get("/api/passport/keys").status_code == 401
    dev = make_user()
    assert client.post("/api/passport/keys", json={}, headers=auth_headers(dev)).status_code == 400


def test_superadmin_keys_still_work_and_are_not_listed(client, make_user, auth_headers):
    admin = AdminUser(name="Ops", email="ops3@example.com", role="SUPERADMIN")
    admin.set_password("not-a-real-password")
    db.session.add(admin)
    db.session.commit()
    admin_headers = {"Authorization": f"Bearer {create_access_token(identity=f'admin:{admin.id}')}"}
    res = client.post("/api/passport/clients", json={"name": "Shopify plugin"}, headers=admin_headers)
    assert res.status_code == 201, res.get_data(as_text=True)
    assert db.session.get(PassportApiClient, res.get_json()["id"]).owner_user_id is None
    # An admin token isn't a developer account, and platform keys can't be revoked via /keys.
    assert client.get("/api/passport/keys", headers=admin_headers).status_code == 403
    dev = make_user()
    assert client.delete(f"/api/passport/keys/{res.get_json()['id']}", headers=auth_headers(dev)).status_code == 404
