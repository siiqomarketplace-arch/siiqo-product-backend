"""Admin two-factor sign-in (TOTP, RFC 6238) — service and routes."""
import base64
import hashlib
import types

import pytest
from flask_jwt_extended import create_access_token

from app.extensions import db
from app.middleware.security import brute_force
from app.models.admin import AdminUser
from app.models.audit import AdminAuditLog
from app.services import totp as totp_service

PASSWORD = "not-a-real-password"


# ---------------------------------------------------------------------------
# RFC 6238 Appendix B test vectors (8 digits, per-algorithm seeds)
# ---------------------------------------------------------------------------

_SEED_SHA1 = b"12345678901234567890"
_SEED_SHA256 = b"12345678901234567890123456789012"
_SEED_SHA512 = b"1234567890123456789012345678901234567890123456789012345678901234"

_RFC6238_VECTORS = [
    # (time, sha1, sha256, sha512)
    (59, "94287082", "46119246", "90693936"),
    (1111111109, "07081804", "68084774", "25091201"),
    (1111111111, "14050471", "67062674", "99943326"),
    (1234567890, "89005924", "91819424", "93441116"),
    (2000000000, "69279037", "90698825", "38618901"),
    (20000000000, "65353130", "77737706", "47863826"),
]


@pytest.mark.parametrize("t,sha1,sha256,sha512", _RFC6238_VECTORS)
def test_rfc6238_vectors(t, sha1, sha256, sha512):
    step = totp_service.time_step(t)
    assert totp_service.hotp(_SEED_SHA1, step, 8, hashlib.sha1) == sha1
    assert totp_service.hotp(_SEED_SHA256, step, 8, hashlib.sha256) == sha256
    assert totp_service.hotp(_SEED_SHA512, step, 8, hashlib.sha512) == sha512


@pytest.mark.parametrize("t,sha1", [(v[0], v[1]) for v in _RFC6238_VECTORS])
def test_totp_six_digit_sha1_matches_vector_suffix(t, sha1):
    secret = base64.b32encode(_SEED_SHA1).decode()
    assert totp_service.totp(secret, for_time=t) == sha1[-6:]


def test_verify_window_and_replay():
    secret = totp_service.generate_secret()
    t = 1_700_000_000
    step = totp_service.time_step(t)
    code = totp_service.totp(secret, for_time=t)
    assert totp_service.verify(secret, code, for_time=t) == step
    assert totp_service.verify(secret, code, for_time=t + 30) == step      # one step late: ok
    assert totp_service.verify(secret, code, for_time=t - 30) == step      # one step early: ok
    assert totp_service.verify(secret, code, for_time=t + 61) is None      # two steps: refused
    assert totp_service.verify(secret, code, for_time=t, last_used_step=step) is None
    assert totp_service.verify(secret, "12345", for_time=t) is None
    assert totp_service.verify(secret, "abcdef", for_time=t) is None


def test_secret_encryption_roundtrip_and_tamper(app):
    with app.app_context():
        secret = totp_service.generate_secret()
        sealed = totp_service.encrypt_secret(secret)
        assert secret not in sealed
        assert totp_service.encrypt_secret(secret) != sealed  # fresh nonce each time
        assert totp_service.decrypt_secret(sealed) == secret
        raw = bytearray(base64.urlsafe_b64decode(sealed.split(":", 1)[1]))
        raw[20] ^= 0x01
        tampered = "v1:" + base64.urlsafe_b64encode(bytes(raw)).decode()
        assert totp_service.decrypt_secret(tampered) is None
        assert totp_service.decrypt_secret("garbage") is None


def test_recovery_code_hashing(app):
    with app.app_context():
        codes = totp_service.generate_recovery_codes()
        assert len(codes) == 10 and len(set(codes)) == 10
        hashes = [totp_service.hash_recovery_code(c) for c in codes]
        assert all(c not in h for c, h in zip(codes, hashes))
        assert totp_service.match_recovery_code(codes[3].upper().replace("-", " "), hashes) == 3
        assert totp_service.match_recovery_code("zzzzz-zzzzz", hashes) is None


# ---------------------------------------------------------------------------
# Route fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _clean_bruteforce():
    brute_force.failed_attempts.clear()
    brute_force.banned_ips.clear()
    yield
    brute_force.failed_attempts.clear()
    brute_force.banned_ips.clear()


@pytest.fixture
def clock(monkeypatch):
    """Deterministic TOTP time; advance with clock.tick(seconds)."""
    state = {"now": 1_700_000_010.0}

    class _Clock:
        def now(self):
            return state["now"]

        def tick(self, seconds):
            state["now"] += seconds

    monkeypatch.setattr(totp_service, "time", types.SimpleNamespace(time=lambda: state["now"]))
    return _Clock()


@pytest.fixture
def make_admin():
    def _make(role="SUPERADMIN", email="root@example.com"):
        admin = AdminUser(name="Root", email=email, role=role)
        admin.set_password(PASSWORD)
        db.session.add(admin)
        db.session.commit()
        return admin
    return _make


def _bearer(admin):
    return {"Authorization": f"Bearer {create_access_token(identity=f'admin:{admin.id}')}"}


def _enroll(client, admin, clock):
    res = client.post("/api/admin/2fa/setup", headers=_bearer(admin))
    assert res.status_code == 200, res.get_json()
    secret = res.get_json()["secret"]
    res = client.post("/api/admin/2fa/enable", headers=_bearer(admin),
                      json={"code": totp_service.totp(secret, clock.now())})
    assert res.status_code == 200, res.get_json()
    return secret, res.get_json()["recovery_codes"]


def _password_step(client, email="root@example.com"):
    return client.post("/api/admin/login", json={"email": email, "password": PASSWORD})


# ---------------------------------------------------------------------------
# Setup / enable / status / disable
# ---------------------------------------------------------------------------

def test_setup_enable_status(client, make_admin, clock):
    admin = make_admin()
    res = client.get("/api/admin/2fa/status", headers=_bearer(admin))
    assert res.get_json()["enabled"] is False

    res = client.post("/api/admin/2fa/setup", headers=_bearer(admin))
    body = res.get_json()
    assert res.headers["Cache-Control"] == "no-store"
    assert body["otpauth_url"].startswith("otpauth://totp/")
    assert f"secret={body['secret']}" in body["otpauth_url"]

    db.session.refresh(admin)
    assert admin.totp_pending_secret_enc and body["secret"] not in admin.totp_pending_secret_enc
    assert not admin.totp_enabled
    assert client.get("/api/admin/2fa/status", headers=_bearer(admin)).get_json()["pending_setup"] is True

    bad = client.post("/api/admin/2fa/enable", headers=_bearer(admin), json={"code": "000000"})
    assert bad.status_code == 400

    res = client.post("/api/admin/2fa/enable", headers=_bearer(admin),
                      json={"code": totp_service.totp(body["secret"], clock.now())})
    assert res.status_code == 200
    codes = res.get_json()["recovery_codes"]
    assert len(codes) == 10

    db.session.refresh(admin)
    assert admin.totp_enabled and admin.totp_pending_secret_enc is None
    assert totp_service.decrypt_secret(admin.totp_secret_enc) == body["secret"]
    assert all(c not in str(admin.totp_recovery_hashes) for c in codes)

    status = client.get("/api/admin/2fa/status", headers=_bearer(admin)).get_json()
    assert status["enabled"] is True and status["recovery_codes_remaining"] == 10
    assert AdminAuditLog.query.filter_by(action="ADMIN_2FA_ENABLED").count() == 1

    # Setup again while enabled is refused.
    assert client.post("/api/admin/2fa/setup", headers=_bearer(admin)).status_code == 409


def test_disable_requires_valid_code(client, make_admin, clock):
    admin = make_admin()
    secret, _ = _enroll(client, admin, clock)
    assert client.post("/api/admin/2fa/disable", headers=_bearer(admin),
                       json={"code": "123456"}).status_code == 400
    clock.tick(30)
    res = client.post("/api/admin/2fa/disable", headers=_bearer(admin),
                      json={"code": totp_service.totp(secret, clock.now())})
    assert res.status_code == 200
    db.session.refresh(admin)
    assert not admin.totp_enabled and admin.totp_secret_enc is None and admin.totp_recovery_hashes is None
    assert AdminAuditLog.query.filter_by(action="ADMIN_2FA_DISABLED").count() == 1


def test_disable_with_recovery_code_is_audited(client, make_admin, clock):
    admin = make_admin()
    _, codes = _enroll(client, admin, clock)
    res = client.post("/api/admin/2fa/disable", headers=_bearer(admin), json={"code": codes[0]})
    assert res.status_code == 200
    assert AdminAuditLog.query.filter_by(action="ADMIN_2FA_RECOVERY_CODE_USED").count() == 1


def test_setup_endpoints_reject_user_tokens(client, make_user):
    user = make_user()
    headers = {"Authorization": f"Bearer {create_access_token(identity=str(user.id))}"}
    for path in ("/api/admin/2fa/setup", "/api/admin/2fa/enable", "/api/admin/2fa/disable"):
        assert client.post(path, headers=headers, json={}).status_code == 403
    assert client.get("/api/admin/2fa/status", headers=headers).status_code == 403


# ---------------------------------------------------------------------------
# Login flow
# ---------------------------------------------------------------------------

def test_login_without_2fa_unchanged(client, make_admin):
    make_admin(role="ADMIN")
    res = _password_step(client)
    body = res.get_json()
    assert res.status_code == 200
    assert set(body) == {"status", "access_token", "admin"}


def test_login_with_totp(client, make_admin, clock):
    admin = make_admin()
    secret, _ = _enroll(client, admin, clock)

    res = _password_step(client)
    body = res.get_json()
    assert res.status_code == 200
    assert body == {"requires_2fa": True, "challenge_token": body["challenge_token"]}
    assert "access_token" not in body

    clock.tick(30)
    res = client.post("/api/admin/login/2fa", json={
        "challenge_token": body["challenge_token"], "code": totp_service.totp(secret, clock.now())})
    out = res.get_json()
    assert res.status_code == 200, out
    assert set(out) == {"status", "access_token", "admin"}
    assert out["status"] == "success"
    assert out["admin"] == {"id": admin.id, "name": "Root", "email": "root@example.com", "role": "SUPERADMIN"}

    # The challenge is single use.
    res = client.post("/api/admin/login/2fa", json={
        "challenge_token": body["challenge_token"], "code": totp_service.totp(secret, clock.now())})
    assert res.status_code == 401


def test_totp_step_cannot_be_replayed(client, make_admin, clock):
    admin = make_admin()
    secret, _ = _enroll(client, admin, clock)
    clock.tick(30)
    code = totp_service.totp(secret, clock.now())

    first = _password_step(client).get_json()["challenge_token"]
    assert client.post("/api/admin/login/2fa", json={"challenge_token": first, "code": code}).status_code == 200

    second = _password_step(client).get_json()["challenge_token"]
    res = client.post("/api/admin/login/2fa", json={"challenge_token": second, "code": code})
    assert res.status_code == 401 and res.get_json()["code"] == "INVALID_2FA_CODE"


def test_enrolment_code_cannot_be_replayed_at_login(client, make_admin, clock):
    admin = make_admin()
    secret, _ = _enroll(client, admin, clock)
    token = _password_step(client).get_json()["challenge_token"]
    res = client.post("/api/admin/login/2fa", json={
        "challenge_token": token, "code": totp_service.totp(secret, clock.now())})
    assert res.status_code == 401


def test_recovery_code_login_is_single_use(client, make_admin, clock):
    admin = make_admin()
    _, codes = _enroll(client, admin, clock)

    token = _password_step(client).get_json()["challenge_token"]
    res = client.post("/api/admin/login/2fa", json={"challenge_token": token, "code": codes[2]})
    assert res.status_code == 200
    assert AdminAuditLog.query.filter_by(action="ADMIN_2FA_RECOVERY_CODE_USED").count() == 1
    db.session.refresh(admin)
    assert len(admin.totp_recovery_hashes) == 9

    token = _password_step(client).get_json()["challenge_token"]
    res = client.post("/api/admin/login/2fa", json={"challenge_token": token, "code": codes[2]})
    assert res.status_code == 401


def test_challenge_locks_after_five_wrong_codes(client, make_admin, clock):
    admin = make_admin()
    secret, _ = _enroll(client, admin, clock)
    clock.tick(30)
    token = _password_step(client).get_json()["challenge_token"]

    for i in range(4):
        res = client.post("/api/admin/login/2fa", json={"challenge_token": token, "code": "000000"})
        assert res.status_code == 401
        assert res.get_json()["attempts_remaining"] == 4 - i
    res = client.post("/api/admin/login/2fa", json={"challenge_token": token, "code": "000000"})
    assert res.status_code == 429 and res.get_json()["code"] == "2FA_LOCKED"

    # Even the right code no longer works on that challenge.
    brute_force.failed_attempts.clear()
    res = client.post("/api/admin/login/2fa", json={
        "challenge_token": token, "code": totp_service.totp(secret, clock.now())})
    assert res.status_code in (401, 429)
    assert AdminAuditLog.query.filter_by(action="ADMIN_2FA_CHALLENGE_LOCKED").count() == 1


def test_challenge_token_is_purpose_bound_and_expires(client, app, make_admin, clock, monkeypatch):
    admin = make_admin()
    secret, _ = _enroll(client, admin, clock)
    clock.tick(30)
    code = totp_service.totp(secret, clock.now())

    # An access token is not a challenge token.
    res = client.post("/api/admin/login/2fa", json={
        "challenge_token": create_access_token(identity=f"admin:{admin.id}"), "code": code})
    assert res.status_code == 401 and res.get_json()["code"] == "CHALLENGE_INVALID"

    # A token signed for another purpose is refused.
    from app.routes import admin as admin_routes
    forged = admin_routes._challenge_serializer().dumps({"aid": admin.id, "n": "x", "p": "other"})
    assert client.post("/api/admin/login/2fa", json={"challenge_token": forged, "code": code}).status_code == 401

    # Expired after five minutes.
    token = _password_step(client).get_json()["challenge_token"]
    monkeypatch.setattr(admin_routes, "_CHALLENGE_MAX_AGE", -1)
    res = client.post("/api/admin/login/2fa", json={"challenge_token": token, "code": code})
    assert res.status_code == 401 and res.get_json()["code"] == "CHALLENGE_INVALID"


def test_new_password_step_invalidates_older_challenge(client, make_admin, clock):
    admin = make_admin()
    secret, _ = _enroll(client, admin, clock)
    clock.tick(30)
    old = _password_step(client).get_json()["challenge_token"]
    _password_step(client)
    res = client.post("/api/admin/login/2fa", json={
        "challenge_token": old, "code": totp_service.totp(secret, clock.now())})
    assert res.status_code == 401


# ---------------------------------------------------------------------------
# SUPERADMIN enforcement
# ---------------------------------------------------------------------------

SUPERADMIN_ONLY = ("patch", "/api/admin/categories/999999", {"name": "x"})


def _call(client, admin):
    method, path, body = SUPERADMIN_ONLY
    return getattr(client, method)(path, json=body, headers=_bearer(admin))


def test_superadmin_without_2fa_is_blocked_when_required(client, make_admin, clock, monkeypatch):
    monkeypatch.setenv("ADMIN_2FA_REQUIRED", "true")
    admin = make_admin()

    # Can still sign in and reach the setup endpoints.
    assert "access_token" in _password_step(client).get_json()
    assert client.get("/api/admin/2fa/status", headers=_bearer(admin)).get_json()["required"] is True

    res = _call(client, admin)
    assert res.status_code == 403 and res.get_json()["code"] == "2FA_REQUIRED"

    _enroll(client, admin, clock)
    res = _call(client, admin)
    assert res.status_code != 403, res.get_json()


def test_enforcement_can_be_switched_off(client, make_admin, monkeypatch):
    monkeypatch.setenv("ADMIN_2FA_REQUIRED", "false")
    admin = make_admin()
    assert _call(client, admin).status_code != 403


def test_enforcement_defaults_on(monkeypatch):
    from app.routes import admin as admin_routes
    monkeypatch.delenv("ADMIN_2FA_REQUIRED", raising=False)
    assert admin_routes._admin_2fa_enforced() is True


def test_non_superadmin_2fa_is_optional(client, make_admin, monkeypatch):
    monkeypatch.setenv("ADMIN_2FA_REQUIRED", "true")
    admin = make_admin(role="ADMIN")
    assert client.get("/api/admin/2fa/status", headers=_bearer(admin)).get_json()["required"] is False
    # Ordinary admins are still refused superadmin actions, but not for 2FA reasons.
    res = _call(client, admin)
    assert res.status_code == 403 and (res.get_json() or {}).get("code") != "2FA_REQUIRED"


def test_daya_admin_routes_require_2fa(client, make_admin, monkeypatch):
    monkeypatch.setenv("ADMIN_2FA_REQUIRED", "true")
    admin = make_admin()
    res = client.get("/api/admin/daya/sweeps", headers=_bearer(admin))
    assert res.status_code == 403
    assert res.get_json()["code"] == "2FA_REQUIRED"
