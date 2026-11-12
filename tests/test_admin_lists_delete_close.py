"""Admin fixes (docs/SCALE_AND_ADMIN_AUDIT_2026-11.md, stage 1):
paged and searchable user/store lists, deleting a seller with payout history,
plain delete errors, and closing/reopening a store."""
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from flask_jwt_extended import create_access_token
from sqlalchemy import event, text
from sqlalchemy.exc import IntegrityError

from app.extensions import db, limiter
from app.models.admin import AdminUser
from app.models.audit import AdminAuditLog
from app.models.communication import Notification
from app.models.escrow import EscrowStatus, EscrowTransaction
from app.models.user import Storefront, User
from app.models.withdrawal import VendorBankAccount, VendorCryptoWallet, Withdrawal

USERS = "/api/admin/users/all"
STORES = "/api/admin/storefronts"


def _admin(role="SUPERADMIN"):
    admin = AdminUser(name="Ops", email=f"ops-{uuid.uuid4().hex[:6]}@example.com", role=role)
    admin.set_password("not-a-real-password")
    db.session.add(admin)
    db.session.commit()
    return {"Authorization": f"Bearer {create_access_token(identity=f'admin:{admin.id}')}"}


def _bulk_users(n, role="BUYER"):
    """n users in one insert, created one second apart (oldest first)."""
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    db.session.execute(User.__table__.insert(), [{
        "email": f"bulk-{i:04d}@example.com", "password_hash": "x", "role": role,
        "first_name": "Bulk", "last_name": f"User{i:04d}", "is_active": True, "is_verified": False,
        "created_at": start + timedelta(seconds=i), "updated_at": start,
    } for i in range(n)])
    db.session.commit()


def _delete(user):
    """Anonymise through the service, as the admin and self-delete routes do."""
    from app.services.accounts import anonymise_user
    anonymise_user(user)


# ── 1. Lists ────────────────────────────────────────────────────────────────

def test_user_list_pages_past_500(client):
    _bulk_users(520)
    headers = _admin()

    first = client.get(USERS, headers=headers).get_json()        # an old client: no page param
    assert first["total"] == 520 and first["page"] == 1 and first["per_page"] == 50
    assert first["pages"] == 11 and len(first["users"]) == 50 and first["count"] == 50
    assert first["users"][0]["email"] == "bulk-0519@example.com"  # newest first
    assert first["counts"]["all"] == 520 and first["counts"]["buyers"] == 520

    seen = set()
    for page in range(1, 4):
        body = client.get(f"{USERS}?page={page}&per_page=200", headers=headers).get_json()
        assert body["per_page"] == 200 and body["pages"] == 3
        seen.update(u["id"] for u in body["users"])
    assert len(seen) == 520                                       # every user reachable, none twice

    last = client.get(f"{USERS}?page=11", headers=headers).get_json()
    assert len(last["users"]) == 20 and last["users"][-1]["email"] == "bulk-0000@example.com"
    assert client.get(f"{USERS}?page=12", headers=headers).get_json()["users"] == []
    assert client.get(f"{USERS}?per_page=9999", headers=headers).get_json()["per_page"] == 200


def test_user_list_search(client, make_user, make_vendor):
    headers = _admin()
    _bulk_users(60)
    ada = make_user(email="Ada.Obi@example.com")
    ada.first_name, ada.last_name, ada.phone = "Adaeze", "Obi", "08031112222"
    vendor, store = make_vendor()
    store.store_name, store.store_slug = "Mama Nkechi Fabrics", "mama-nkechi"
    db.session.commit()

    def ids(q):
        body = client.get(USERS, query_string={"q": q}, headers=headers).get_json()
        assert body["total"] == len(body["users"])
        return {u["id"] for u in body["users"]}

    assert ids("ada.obi@") == {ada.id}             # email, case-insensitive
    assert ids("adaeze obi") == {ada.id}           # full name
    assert ids("0803111") == {ada.id}              # phone
    assert ids("nkechi fab") == {vendor.id}        # store name
    assert ids("mama-nkechi") == {vendor.id}       # store slug
    assert ada.id in ids(str(ada.id))              # exact id
    assert ids("100%_nobody") == set()             # wildcards are literal
    # counts ignore the search
    body = client.get(USERS, query_string={"q": "adaeze"}, headers=headers).get_json()
    assert body["total"] == 1 and body["counts"]["all"] == 62


def test_user_list_status_role_and_counts(client, make_user, make_vendor):
    headers = _admin()
    buyer = make_user()                                    # unverified buyer
    verified_buyer = make_user()
    verified_buyer.is_verified = True
    suspended = make_user()
    suspended.is_active = False
    pending_v, pending_s = make_vendor()
    pending_s.verification_status = "PENDING_VERIFY_SUB"
    verified_v, verified_s = make_vendor()
    verified_s.verification_status = "VERIFIED"
    rejected_v, rejected_s = make_vendor()
    rejected_s.verification_status = "REJECTED"
    gone = make_user()
    db.session.commit()
    _delete(gone)

    def ids(**params):
        body = client.get(USERS, query_string=params, headers=headers).get_json()
        assert body["total"] == len(body["users"])
        return {u["id"] for u in body["users"]}

    everyone = {buyer.id, verified_buyer.id, suspended.id, pending_v.id, verified_v.id, rejected_v.id}
    assert ids() == everyone                                # deleted hidden by default
    assert ids(include_deleted="true") == everyone | {gone.id}
    assert ids(status="deleted") == {gone.id}
    assert ids(status="suspended") == {suspended.id}        # a deleted user is not "suspended"
    assert ids(status="suspended", include_deleted="true") == {suspended.id}
    assert ids(status="active") == everyone - {suspended.id}
    assert ids(status="pending") == {pending_v.id}
    assert ids(status="verified") == {verified_buyer.id, verified_v.id}
    assert ids(status="rejected") == {rejected_v.id}
    assert ids(role="vendor") == {pending_v.id, verified_v.id, rejected_v.id}
    assert ids(role="buyer", status="active") == {buyer.id, verified_buyer.id}
    assert client.get(f"{USERS}?status=nonsense", headers=headers).status_code == 400

    body = client.get(USERS, query_string={"include_deleted": "true"}, headers=headers).get_json()
    assert body["counts"] == {"all": 6, "active": 5, "suspended": 1, "pending": 1, "verified": 2,
                              "vendors": 3, "buyers": 3, "deleted": 1}
    by_id = {u["id"]: u for u in body["users"]}
    # The SQL status filters agree with the status shown on each row.
    assert by_id[gone.id]["is_deleted"] is True and by_id[gone.id]["status"] == "deleted"
    assert by_id[buyer.id]["is_deleted"] is False and by_id[buyer.id]["status"] == "unverified"
    assert by_id[suspended.id]["status"] == "suspended"
    assert by_id[pending_v.id]["status"] == "pending"
    assert by_id[verified_v.id]["status"] == by_id[verified_buyer.id]["status"] == "verified"
    assert by_id[verified_v.id]["store_slug"] == verified_s.store_slug
    assert by_id[verified_v.id]["market_presence"]["product_count"] == 0


def test_user_list_needs_an_admin(client, make_user, auth_headers):
    assert client.get(USERS, headers=auth_headers(make_user())).status_code == 403


def test_user_list_query_count_does_not_grow_with_the_page(client, make_vendor, make_product, app):
    headers = _admin()
    for _ in range(12):
        _, store = make_vendor()
        make_product(store)
    statements = []

    def _count(conn, cursor, statement, *args):
        statements.append(statement)

    event.listen(db.engine, "before_cursor_execute", _count)
    try:
        body = client.get(USERS, headers=headers).get_json()
    finally:
        event.remove(db.engine, "before_cursor_execute", _count)
    assert len(body["users"]) == 12
    assert all(u["market_presence"]["product_count"] == 1 for u in body["users"])
    assert len(statements) <= 8, statements       # admin lookup, total, page, product counts, counts


def test_store_list_pages_and_searches(client, make_vendor):
    headers = _admin()
    stores = []
    for i in range(7):
        vendor, store = make_vendor()
        store.store_name, store.city, store.state = f"Shop {i}", "Wuse", "FCT"
        stores.append(store)
    owner, target = make_vendor()
    owner.email = "owner.special@example.com"
    target.store_name, target.store_slug, target.city, target.state = "Kano Leather", "kano-leather", "Kano", "Kano"
    db.session.commit()

    body = client.get(f"{STORES}?per_page=3", headers=headers).get_json()
    assert body["count"] == 8 and body["pages"] == 3 and body["page"] == 1 and body["per_page"] == 3
    assert len(body["storefronts"]) == 3 and body["storefronts"][0]["is_closed"] is False
    seen = set()
    for page in (1, 2, 3):
        seen.update(s["id"] for s in client.get(f"{STORES}?per_page=3&page={page}", headers=headers)
                    .get_json()["storefronts"])
    assert len(seen) == 8

    default = client.get(STORES, headers=headers).get_json()      # an old client
    assert default["count"] == 8 and default["per_page"] == 50 and len(default["storefronts"]) == 8

    def ids(**params):
        return {s["id"] for s in client.get(STORES, query_string=params, headers=headers).get_json()["storefronts"]}

    assert ids(q="leather") == {target.id}
    assert ids(q="KANO-LEA") == {target.id}
    assert ids(q="owner.special") == {target.id}
    assert ids(q="wuse") == {s.id for s in stores}
    assert ids(q="kano") == {target.id}
    target.verification_status = "VERIFIED"
    db.session.commit()
    assert ids(status="VERIFIED") == {target.id}                  # the old filter still works
    assert ids(status="VERIFIED", q="wuse") == set()


# ── 2. Delete user ──────────────────────────────────────────────────────────

@pytest.fixture
def enforce_foreign_keys(app):
    """SQLite ignores foreign keys unless asked; Postgres always enforces them.
    Turn enforcement on for this test's connection."""
    db.session.remove()
    db.session.execute(text("PRAGMA foreign_keys=ON"))
    assert db.session.execute(text("PRAGMA foreign_keys")).scalar() == 1
    yield
    db.session.rollback()
    db.session.execute(text("PRAGMA foreign_keys=OFF"))
    assert db.session.execute(text("PRAGMA foreign_keys")).scalar() == 0
    db.session.commit()
    db.session.remove()


def _seller_with_payout(make_vendor):
    vendor, store = make_vendor()
    used = VendorBankAccount(vendor_id=vendor.id, bank_name="GTBank", bank_code="058",
                             account_number="0123456789", account_name="Test User",
                             recipient_code=f"RCP_{uuid.uuid4().hex[:8]}", is_verified=True, is_default=True)
    spare = VendorBankAccount(vendor_id=vendor.id, bank_name="Access", bank_code="044",
                              account_number="0987654321", account_name="Test User")
    db.session.add_all([used, spare])
    db.session.flush()
    payout = Withdrawal(vendor_id=vendor.id, bank_account_id=used.id, amount=5000, net_amount=4950,
                        status="COMPLETED")
    db.session.add_all([
        payout,
        VendorCryptoWallet(vendor_id=vendor.id, wallet_address="T" + "x" * 33),
        Notification(user_id=vendor.id, title="Hello", message="Hi"),
    ])
    db.session.commit()
    return vendor, store, used.id, spare.id, payout.id


def test_foreign_keys_are_really_enforced_in_this_test(enforce_foreign_keys, make_vendor):
    """Guards the fixture: the old code path (delete the paid-to bank account) must fail here."""
    vendor, _, used_id, _, _ = _seller_with_payout(make_vendor)
    with pytest.raises(IntegrityError):
        VendorBankAccount.query.filter_by(vendor_id=vendor.id).delete(synchronize_session=False)
        db.session.commit()
    db.session.rollback()


def test_seller_with_payout_history_can_be_deleted(client, enforce_foreign_keys, make_vendor):
    vendor, store, used_id, spare_id, payout_id = _seller_with_payout(make_vendor)
    headers = _admin()

    res = client.delete(f"/api/admin/users/{vendor.id}", headers=headers)
    assert res.status_code == 200, res.get_json()
    assert res.get_json() == {
        "message": "User deleted. Their personal details are removed and their login is disabled. "
                   "Orders, payouts and trust records are kept.",
        "user_id": vendor.id,
    }

    db.session.expire_all()
    assert db.session.execute(text("PRAGMA foreign_keys")).scalar() == 1   # still enforced after the request
    assert db.session.get(User, vendor.id).email == f"deleted-{vendor.id}@deleted.siiqo.invalid"
    # The payout and the account it was paid to stay; the account is scrubbed.
    payout = db.session.get(Withdrawal, payout_id)
    assert payout is not None and payout.bank_account_id == used_id
    kept = db.session.get(VendorBankAccount, used_id)
    assert kept.account_number == "******6789" and kept.account_name == "Deleted user"
    assert kept.bank_name == "GTBank" and kept.recipient_code is None
    assert kept.is_default is False and kept.is_verified is False
    # The account with no payout history, the wallet and notifications are gone.
    assert db.session.get(VendorBankAccount, spare_id) is None
    assert VendorCryptoWallet.query.filter_by(vendor_id=vendor.id).count() == 0
    assert Notification.query.filter_by(user_id=vendor.id).count() == 0
    sf = db.session.get(Storefront, store.id)
    assert sf.is_published is False and sf.is_closed is True
    assert AdminAuditLog.query.filter_by(action="USER_DELETE", resource_id=str(vendor.id)).count() == 1

    # A second delete is refused, and the user now shows as deleted in the list.
    again = client.delete(f"/api/admin/users/{vendor.id}", headers=headers)
    assert again.status_code == 409 and again.get_json()["code"] == "ALREADY_DELETED"
    listed = client.get(f"{USERS}?status=deleted", headers=headers).get_json()
    assert [u["id"] for u in listed["users"]] == [vendor.id]


def test_delete_errors_carry_a_code_and_a_plain_message(client, make_user, make_vendor, make_order, monkeypatch):
    victim = make_user()

    res = client.delete(f"/api/admin/users/{victim.id}", headers=_admin(role="ADMIN"))
    assert res.status_code == 403
    assert res.get_json() == {"code": "SUPERADMIN_REQUIRED", "message": "Only a Super Admin can delete users."}

    headers = _admin()
    monkeypatch.setenv("ADMIN_2FA_REQUIRED", "true")
    res = client.delete(f"/api/admin/users/{victim.id}", headers=headers)
    assert res.status_code == 403
    assert res.get_json() == {"code": "2FA_REQUIRED",
                              "message": "Turn on two-factor login for your admin account to do this."}
    monkeypatch.setenv("ADMIN_2FA_REQUIRED", "false")

    vendor, _ = make_vendor()
    order = make_order(vendor, buyer=victim, total=1000)
    escrow = EscrowTransaction(order_id=order.id, transaction_number=f"T-{order.id}",
                               status=EscrowStatus.PENDING_PAYMENT, amount=1000, fee_amount=30)
    db.session.add(escrow)
    db.session.commit()
    escrow.status = EscrowStatus.IN_ESCROW
    db.session.commit()
    res = client.delete(f"/api/admin/users/{victim.id}", headers=headers)
    assert res.status_code == 409
    assert res.get_json() == {
        "code": "OPEN_ORDERS", "open_orders": 1,
        "message": "This user has open orders. Finish or cancel them first, or delete anyway."}
    assert db.session.get(User, victim.id).is_active is True

    assert client.delete("/api/admin/users/999999", headers=headers).status_code == 404

    # A failure part-way leaves nothing changed, audit row included.
    email = victim.email

    def _boom(*args, **kwargs):
        raise RuntimeError("audit store down")

    monkeypatch.setattr(AdminAuditLog, "log_action", staticmethod(_boom))
    res = client.delete(f"/api/admin/users/{victim.id}?force=true", headers=headers)
    assert res.status_code == 500
    assert res.get_json() == {"code": "DELETE_FAILED",
                              "message": "We couldn't delete this user. Nothing was changed."}
    db.session.expire_all()
    assert db.session.get(User, victim.id).email == email
    monkeypatch.undo()

    res = client.delete(f"/api/admin/users/{victim.id}?force=true", headers=headers)
    assert res.status_code == 200 and res.get_json()["user_id"] == victim.id


def test_delete_rate_limit_is_60_an_hour_and_answers_in_json(client, make_user):
    from flask_limiter.util import get_qualified_name

    from app.routes import admin as admin_routes
    limits = [str(lim.limit) for lim in
              limiter.limit_manager.decorated_limits(get_qualified_name(admin_routes.delete_user_admin))]
    assert limits == ["60 per 1 hour"]

    headers = _admin()
    limiter.enabled = True
    try:
        limiter.reset()
        codes = [client.delete("/api/admin/users/999999", headers=headers).status_code for _ in range(60)]
        assert set(codes) == {404}
        res = client.delete("/api/admin/users/999999", headers=headers)
        assert res.status_code == 429
        assert res.get_json() == {"code": "RATE_LIMITED",
                                  "message": "Too many deletions in a short time. Wait a while and try again."}
    finally:
        limiter.reset()
        limiter.enabled = False


# ── 3. Close / reopen a store ───────────────────────────────────────────────

def _publish(store):
    store.is_published = True
    db.session.commit()


def test_close_hides_the_store_and_reopen_restores_it(client, make_vendor, make_product, auth_headers):
    vendor, store = make_vendor()
    product = make_product(store)
    _publish(store)
    slug = store.store_slug
    headers = _admin(role="ADMIN")                       # any admin, not only a Super Admin

    assert client.get(f"/api/marketplace/store/{slug}").status_code == 200
    res = client.post(f"{STORES}/{store.id}/close", json={"reason": "Selling banned goods"}, headers=headers)
    assert res.status_code == 200, res.get_json()
    body = res.get_json()
    assert body["storefront_id"] == store.id and body["is_closed"] is True and body["message"]

    db.session.expire_all()
    sf = db.session.get(Storefront, store.id)
    assert sf.is_published is False and sf.closed_at is not None and sf.is_live is False
    assert sf.store_slug == slug and len(sf.products) == 1 and sf.products[0].id == product.id
    note = Notification.query.filter_by(user_id=vendor.id).one()
    assert note.message == "Your store was closed by Siiqo. Contact support if you think this is a mistake."
    log = AdminAuditLog.query.filter_by(action="STOREFRONT_CLOSE", resource_id=str(store.id)).one()
    assert log.details["reason"] == "Selling banned goods" and log.details["was_published"] is True

    listed = client.get(STORES, headers=headers).get_json()["storefronts"]
    assert listed[0]["is_closed"] is True and listed[0]["is_published"] is False

    again = client.post(f"{STORES}/{store.id}/close", headers=headers)
    assert again.status_code == 409 and again.get_json()["code"] == "ALREADY_CLOSED"

    # The seller can't publish it again, but can still save other settings.
    res = client.patch("/api/vendor/update-settings", json={"is_published": True}, headers=auth_headers(vendor))
    assert res.status_code == 403 and res.get_json()["code"] == "STORE_CLOSED", res.get_json()
    res = client.patch("/api/vendor/update-settings", json={"theme_color": "#112233"}, headers=auth_headers(vendor))
    assert res.status_code == 200, res.get_json()
    db.session.expire_all()
    assert db.session.get(Storefront, store.id).is_published is False
    assert client.get("/api/vendor/settings", headers=auth_headers(vendor)).status_code == 200

    res = client.post(f"{STORES}/{store.id}/reopen", headers=headers)
    assert res.status_code == 200, res.get_json()
    assert res.get_json()["is_closed"] is False and res.get_json()["is_published"] is True
    db.session.expire_all()
    sf = db.session.get(Storefront, store.id)
    assert sf.closed_at is None and sf.is_published is True
    assert AdminAuditLog.query.filter_by(action="STOREFRONT_REOPEN", resource_id=str(store.id)).count() == 1

    again = client.post(f"{STORES}/{store.id}/reopen", headers=headers)
    assert again.status_code == 409 and again.get_json()["code"] == "NOT_CLOSED"


def test_closed_store_public_page_matches_an_unpublished_one(client, make_vendor):
    _, never_published = make_vendor()
    _, closed = make_vendor()
    _publish(closed)
    assert client.post(f"{STORES}/{closed.id}/close", headers=_admin()).status_code == 200

    def public(slug):
        return client.get(f"/api/marketplace/store/{slug}")

    a, b = public(never_published.store_slug), public(closed.store_slug)
    assert a.status_code == b.status_code == 202
    assert a.get_json() == b.get_json() == {"status": "offline", "message": "This storefront is currently offline."}


def test_reopen_does_not_publish_a_store_that_was_not_published(client, make_vendor):
    _, store = make_vendor()                             # never published
    headers = _admin()
    assert client.post(f"{STORES}/{store.id}/close", headers=headers).status_code == 200
    res = client.post(f"{STORES}/{store.id}/reopen", headers=headers)
    assert res.status_code == 200 and res.get_json()["is_published"] is False
    db.session.expire_all()
    sf = db.session.get(Storefront, store.id)
    assert sf.closed_at is None and sf.is_published is False


def test_close_and_reopen_edge_cases(client, make_vendor, make_user, auth_headers):
    headers = _admin()
    assert client.post(f"{STORES}/999999/close", headers=headers).status_code == 404
    assert client.post(f"{STORES}/999999/reopen", headers=headers).status_code == 404

    vendor, store = make_vendor()
    assert client.post(f"{STORES}/{store.id}/close", headers=auth_headers(make_user())).status_code == 403
    assert client.post(f"{STORES}/{store.id}/close").status_code == 401

    # A deleted owner's store shows as closed and can't be reopened.
    _delete(vendor)
    res = client.post(f"{STORES}/{store.id}/reopen", headers=headers)
    assert res.status_code == 409 and res.get_json()["code"] == "OWNER_DELETED"
    res = client.post(f"{STORES}/{store.id}/close", headers=headers)
    assert res.status_code == 409 and res.get_json()["code"] == "ALREADY_CLOSED"


def test_bulk_publish_and_verification_release_leave_closed_stores_closed(client, make_vendor):
    headers = _admin()
    _, store = make_vendor()
    store.is_verified = True
    db.session.commit()
    assert client.post(f"{STORES}/{store.id}/close", headers=headers).status_code == 200
    assert client.post(f"{STORES}/publish-all-approved", headers=headers).status_code == 200
    db.session.expire_all()
    sf = db.session.get(Storefront, store.id)
    assert sf.is_published is False and sf.closed_at is not None
