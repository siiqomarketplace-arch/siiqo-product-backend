"""Business directory and claim flow (Phase 3 item 3-5): import dry run and
apply with duplicates, audit, the unclaimed /check result, claims by phone
code (mocked sender) and by domain (mocked DNS), removal requests, the /start
name-check match, and no personal-data fields."""
import pytest
from flask_jwt_extended import create_access_token

from app.extensions import db
from app.models.admin import AdminUser
from app.models.audit import AdminAuditLog
from app.models.directory import (BusinessListing, BusinessListingIdentifier, DirectoryAuditEvent,
                                  DirectoryClaim)
from app.models.foundations import SellerIdentifier
from app.models.platform import OwnershipClaim
from app.services import directory, dns_txt
from app.services.passport import settings_for

CSV = (
    "name,category,city,state,phone,website,social_network,social_handle,owner_email,owner_name\n"
    "Ada Cakes,Bakery,Wuse,Abuja,0803 111 2222,https://www.adacakes.ng,instagram,@adacakes,ada@gmail.com,Ada Obi\n"
    "Bola Prints,Printing,Ikeja,Lagos,,,instagram,https://instagram.com/bolaprints,,\n"
    "Existing Store Twin,Fashion,Yaba,Lagos,08035550000,,,,,\n"
    "Ada Cakes Copy,Bakery,Wuse,Abuja,+2348031112222,,,,,\n"
    "No Contact Ltd,Retail,Kano,Kano,,,,,,\n"
    "Bad Phone,Retail,Kano,Kano,12345,,,,,\n"
)


def _superadmin(role="SUPERADMIN"):
    admin = AdminUser(name="Ops", email=f"ops-{role.lower()}-{db.session.query(AdminUser).count()}@siiqo.test",
                      role=role)
    admin.set_password("not-a-real-password")
    db.session.add(admin)
    db.session.commit()
    return admin, {"Authorization": f"Bearer {create_access_token(identity=f'admin:{admin.id}')}"}


@pytest.fixture
def existing_store(make_vendor):
    vendor, store = make_vendor()
    db.session.add(SellerIdentifier(vendor_id=vendor.id, kind="phone", value="2348035550000", source="store"))
    db.session.commit()
    return vendor, store


def _dry_run(client, headers, csv=CSV, **extra):
    body = {"csv": csv, "source": "admin_import", "source_note": "Lagos SME association list, shared with consent",
            **extra}
    return client.post("/api/admin/directory/import/dry-run", json=body, headers=headers)


def _import(client, csv=CSV):
    _, headers = _superadmin()
    res = _dry_run(client, headers, csv=csv)
    assert res.status_code == 201, res.get_json()
    batch = res.get_json()["batch"]
    res = client.post(f"/api/admin/directory/import/{batch['id']}/apply", headers=headers)
    assert res.status_code == 200, res.get_json()
    return batch, res.get_json()


def _listing(name):
    return BusinessListing.query.filter_by(name=name).first()


# ── import ─────────────────────────────────────────────────────────────────

def test_dry_run_shows_adds_duplicates_and_invalid_rows_without_writing(client, existing_store):
    vendor, store = existing_store
    _, headers = _superadmin()
    res = _dry_run(client, headers)
    assert res.status_code == 201
    batch = res.get_json()["batch"]
    by_line = {r["line"]: r for r in batch["rows"]}

    assert by_line[2]["action"] == "add"
    assert by_line[2]["fields"]["phone"] == "2348031112222"
    assert by_line[2]["fields"]["website"] == "adacakes.ng"
    assert by_line[2]["identifiers"] == [["phone", "2348031112222"], ["domain", "adacakes.ng"],
                                         ["handle", "instagram:adacakes"]]
    assert by_line[3]["action"] == "add"
    assert by_line[3]["fields"]["social_handle"] == "bolaprints"
    # Duplicate of an existing Siiqo store, matched by phone.
    assert by_line[4]["action"] == "duplicate"
    assert by_line[4]["duplicate_stores"][0]["vendor_id"] == vendor.id
    assert by_line[4]["duplicate_stores"][0]["store_slug"] == store.store_slug
    # Duplicate of another row in the same file (same phone, different format).
    assert by_line[5]["action"] == "duplicate"
    assert by_line[5]["duplicate_lines"] == [2]
    assert by_line[6]["action"] == "invalid" and by_line[6]["errors"] == ["no_contact"]
    assert by_line[7]["action"] == "invalid" and "phone_invalid" in by_line[7]["errors"]

    summary = batch["summary"]
    assert (summary["add"], summary["duplicate"], summary["invalid"], summary["total"]) == (2, 2, 2, 6)
    assert summary["ignored_columns"] == ["owner_email", "owner_name"]
    # A dry run writes no listing.
    assert BusinessListing.query.count() == 0


def test_apply_adds_listings_and_identifiers_and_detects_existing_listings(client, existing_store):
    batch, applied = _import(client)
    assert applied["added"] == 2
    ada = _listing("Ada Cakes")
    assert ada.status == "unclaimed" and ada.source == "admin_import"
    assert ada.source_note == "Lagos SME association list, shared with consent"
    idents = {(i.kind, i.value, i.source) for i in BusinessListingIdentifier.query.filter_by(listing_id=ada.id)}
    assert idents == {("phone", "2348031112222", "directory"), ("domain", "adacakes.ng", "directory"),
                      ("handle", "instagram:adacakes", "directory")}

    # A second import of the same business is a duplicate of the listing.
    _, headers = _superadmin()
    res = _dry_run(client, headers, csv="name,website\nAda Cakes Again,adacakes.ng\n")
    row = res.get_json()["batch"]["rows"][0]
    assert row["action"] == "duplicate"
    assert row["duplicate_listings"][0]["id"] == ada.id


def test_apply_only_once_and_rechecks_duplicates(client, make_vendor):
    _, headers = _superadmin()
    batch = _dry_run(client, headers, csv="name,phone\nLate Twin,08036660000\n").get_json()["batch"]
    # A store with the same phone appears between the dry run and apply.
    vendor, _ = make_vendor()
    db.session.add(SellerIdentifier(vendor_id=vendor.id, kind="phone", value="2348036660000", source="store"))
    db.session.commit()
    res = client.post(f"/api/admin/directory/import/{batch['id']}/apply", headers=headers)
    assert res.get_json()["added"] == 0
    assert res.get_json()["batch"]["summary"]["applied_skipped_lines"] == [2]
    again = client.post(f"/api/admin/directory/import/{batch['id']}/apply", headers=headers)
    assert again.status_code == 409


def test_import_requires_superadmin_and_source_note(client, make_user, auth_headers):
    _, admin_headers = _superadmin(role="ADMIN")
    assert _dry_run(client, admin_headers).status_code == 403
    assert _dry_run(client, auth_headers(make_user())).status_code == 403
    _, headers = _superadmin()
    res = _dry_run(client, headers, source_note="")
    assert res.status_code == 400 and res.get_json()["code"] == "SOURCE_NOTE_REQUIRED"
    res = _dry_run(client, headers, source="scraped")
    assert res.status_code == 400 and res.get_json()["code"] == "INVALID_SOURCE"


def test_import_accepts_multipart_upload(client):
    import io
    _, headers = _superadmin()
    data = {"file": (io.BytesIO(b"name,phone\nUpload Co,08037770000\n"), "list.csv"),
            "source": "partner", "source_note": "Partner feed from Example Association"}
    res = client.post("/api/admin/directory/import/dry-run", data=data, headers=headers,
                      content_type="multipart/form-data")
    assert res.status_code == 201, res.get_json()
    batch = res.get_json()["batch"]
    assert batch["source"] == "partner" and batch["filename"] == "list.csv"
    assert batch["summary"]["add"] == 1


def test_import_and_removal_are_audit_logged(client, existing_store):
    batch, _ = _import(client)
    events = [e.event for e in DirectoryAuditEvent.query.order_by(DirectoryAuditEvent.id)]
    assert events == ["DIRECTORY_IMPORT_DRY_RUN", "DIRECTORY_IMPORT_APPLIED"]
    admin_actions = [a.action for a in AdminAuditLog.query.order_by(AdminAuditLog.id)]
    assert admin_actions == ["DIRECTORY_IMPORT_DRY_RUN", "DIRECTORY_IMPORT_APPLIED"]
    applied = DirectoryAuditEvent.query.filter_by(event="DIRECTORY_IMPORT_APPLIED").one()
    assert applied.details["source_note"] == "Lagos SME association list, shared with consent"
    assert applied.details["added"] == 2

    ada = _listing("Ada Cakes")
    _, headers = _superadmin()
    assert client.post(f"/api/admin/directory/listings/{ada.id}/remove", json={}, headers=headers).status_code == 400
    res = client.post(f"/api/admin/directory/listings/{ada.id}/remove", json={"reason": "Owner asked by email"},
                      headers=headers)
    assert res.status_code == 200
    assert _listing("Ada Cakes").status == "removed"
    assert BusinessListingIdentifier.query.filter_by(listing_id=ada.id).count() == 0
    removed = DirectoryAuditEvent.query.filter_by(event="DIRECTORY_LISTING_REMOVED").one()
    assert removed.actor_type == "ADMIN" and removed.details["reason"] == "Owner asked by email"
    assert AdminAuditLog.query.filter_by(action="DIRECTORY_LISTING_REMOVED", resource_id=str(ada.id)).count() == 1

    listed = client.get("/api/admin/directory/audit", query_string={"listing_id": ada.id}, headers=headers)
    assert [e["event"] for e in listed.get_json()["events"]] == ["DIRECTORY_LISTING_REMOVED"]


# ── /check ─────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("q", ["0803 111 2222", "adacakes.ng", "https://instagram.com/adacakes", "@adacakes",
                               "Ada Cakes"])
def test_check_returns_unclaimed_listing_with_public_fields_only(client, q):
    _import(client)
    res = client.get("/api/check", query_string={"q": q})
    assert res.status_code == 200
    data = res.get_json()
    assert data["found"] is False and data["results"] == []
    assert data["status"] == "unclaimed"
    listing = data["listing"]
    assert set(listing) == {"id", "name", "category", "city", "state", "status", "on_siiqo", "verified"}
    assert listing["name"] == "Ada Cakes" and listing["verified"] is False
    body = res.get_data(as_text=True)
    assert "2348031112222" not in body and "SME association" not in body
    assert "scam" not in data["message"].lower() and "safe" not in data["message"].lower()


def test_check_prefers_a_siiqo_seller_and_ignores_unknown(client, make_vendor):
    _import(client)
    vendor, store = make_vendor()
    settings_for(vendor.id)
    db.session.add(SellerIdentifier(vendor_id=vendor.id, kind="domain", value="adacakes.ng", source="store"))
    db.session.commit()
    data = client.get("/api/check", query_string={"q": "adacakes.ng"}).get_json()
    assert "status" not in data
    assert client.get("/api/check", query_string={"q": "nobody.ng"}).get_json().get("status") is None


# ── claims ─────────────────────────────────────────────────────────────────

class _FakeSms:
    def __init__(self):
        self.sent = []

    def __call__(self, phone, text):
        self.sent.append((phone, text))
        return True

    @property
    def code(self):
        import re
        return re.search(r"\b(\d{6})\b", self.sent[-1][1]).group(1)


def test_phone_claim_is_coming_soon_without_a_sender(client, make_user, auth_headers):
    _import(client)
    ada = _listing("Ada Cakes")
    view = client.get(f"/api/directory/listings/{ada.id}").get_json()["listing"]
    assert {m["method"]: m["available"] for m in view["methods"]} == {"phone": False, "domain": True,
                                                                      "handle": True}
    assert "2348031112222" not in str(view)
    res = client.post(f"/api/directory/listings/{ada.id}/claim", json={"method": "phone"},
                      headers=auth_headers(make_user()))
    assert res.status_code == 503 and res.get_json()["code"] == "OTP_UNAVAILABLE"


def test_claim_by_phone_code_links_store_and_leads_to_verification(client, make_vendor, auth_headers,
                                                                   monkeypatch):
    sms = _FakeSms()
    monkeypatch.setattr(directory, "otp_sender", lambda: sms)
    _import(client)
    ada = _listing("Ada Cakes")
    vendor, store = make_vendor()

    assert client.post(f"/api/directory/listings/{ada.id}/claim", json={"method": "phone"}).status_code == 401
    res = client.post(f"/api/directory/listings/{ada.id}/claim", json={"method": "phone"},
                      headers=auth_headers(vendor))
    assert res.status_code == 201, res.get_json()
    claim = res.get_json()["claim"]
    assert claim["status"] == "pending" and sms.sent[0][0] == "2348031112222"

    wrong = client.post(f"/api/directory/claims/{claim['id']}/code", json={"code": "000000"},
                        headers=auth_headers(vendor))
    if sms.code != "000000":
        assert wrong.status_code == 400
    res = client.post(f"/api/directory/claims/{claim['id']}/code", json={"code": sms.code},
                      headers=auth_headers(vendor))
    assert res.status_code == 200, res.get_json()
    body = res.get_json()["claim"]
    assert body["status"] == "verified"
    assert body["next"] == {"action": "verify", "href": "/vendor/trust/verify"}

    ada = _listing("Ada Cakes")
    assert ada.status == "claimed" and ada.claimed_storefront_id == store.id and ada.claimed_user_id == vendor.id
    graph = {(i.kind, i.value) for i in SellerIdentifier.query.filter_by(vendor_id=vendor.id, source="directory")}
    assert ("phone", "2348031112222") in graph and ("domain", "adacakes.ng") in graph
    assert DirectoryAuditEvent.query.filter_by(event="DIRECTORY_LISTING_CLAIMED", listing_id=ada.id).count() == 1

    # Claimed listings never show as unclaimed again.
    data = client.get("/api/check", query_string={"q": "Ada Cakes"}).get_json()
    assert data.get("status") != "unclaimed"
    data = client.get("/api/check", query_string={"q": "0803 111 2222"}).get_json()
    assert data.get("status") != "unclaimed"
    # And can't be claimed twice.
    again = client.post(f"/api/directory/listings/{ada.id}/claim", json={"method": "domain"},
                        headers=auth_headers(vendor))
    assert again.status_code == 409


def test_phone_code_attempts_and_other_users(client, make_user, auth_headers, monkeypatch):
    sms = _FakeSms()
    monkeypatch.setattr(directory, "otp_sender", lambda: sms)
    _import(client)
    ada = _listing("Ada Cakes")
    owner, stranger = make_user(), make_user()
    claim = client.post(f"/api/directory/listings/{ada.id}/claim", json={"method": "phone"},
                        headers=auth_headers(owner)).get_json()["claim"]
    # Someone else can't see or use the claim.
    assert client.get(f"/api/directory/claims/{claim['id']}", headers=auth_headers(stranger)).status_code == 404
    bad = "111111" if sms.code != "111111" else "222222"
    for _ in range(directory.OTP_MAX_ATTEMPTS):
        client.post(f"/api/directory/claims/{claim['id']}/code", json={"code": bad}, headers=auth_headers(owner))
    res = client.post(f"/api/directory/claims/{claim['id']}/code", json={"code": sms.code},
                      headers=auth_headers(owner))
    assert res.status_code == 409
    assert _listing("Ada Cakes").status == "unclaimed"


def test_claim_by_domain_without_store_then_link_after_start(client, make_user, auth_headers, monkeypatch):
    _import(client)
    ada = _listing("Ada Cakes")
    user = make_user()
    res = client.post(f"/api/directory/listings/{ada.id}/claim", json={"method": "domain"},
                      headers=auth_headers(user))
    assert res.status_code == 201
    claim = res.get_json()["claim"]
    row = DirectoryClaim.query.filter_by(public_id=claim["id"]).one()
    assert claim["instructions"]["dns"] == {"type": "TXT", "host": "adacakes.ng",
                                           "value": f"siiqo-verify={row.token}"}

    monkeypatch.setattr(dns_txt, "txt_records", lambda name: [])
    monkeypatch.setattr("app.services.ownership._check_meta", lambda c: False)
    res = client.post(f"/api/directory/claims/{claim['id']}/check", headers=auth_headers(user))
    assert res.status_code == 202 and res.get_json()["claim"]["status"] == "pending"

    monkeypatch.setattr(dns_txt, "txt_records", lambda name: [f"siiqo-verify={row.token}"])
    res = client.post(f"/api/directory/claims/{claim['id']}/check", headers=auth_headers(user))
    body = res.get_json()["claim"]
    assert body["status"] == "verified"
    assert body["next"] == {"action": "create_store", "href": "/start?name=Ada%20Cakes"}
    ada = _listing("Ada Cakes")
    assert ada.status == "claimed" and ada.claimed_storefront_id is None

    # Link once the store exists (after /start).
    assert client.post(f"/api/directory/claims/{claim['id']}/link", headers=auth_headers(user)).status_code == 409
    from app.models.user import Storefront
    store = Storefront(vendor_id=user.id, store_name="Ada Cakes", store_slug="ada-cakes")
    db.session.add(store)
    db.session.commit()
    res = client.post(f"/api/directory/claims/{claim['id']}/link", headers=auth_headers(user))
    assert res.status_code == 200
    assert res.get_json()["claim"]["next"]["action"] == "verify"
    assert _listing("Ada Cakes").claimed_storefront_id == store.id


def test_claim_uses_an_already_verified_ownership(client, make_vendor, auth_headers):
    _import(client)
    ada = _listing("Ada Cakes")
    vendor, _ = make_vendor()
    db.session.add(OwnershipClaim(vendor_id=vendor.id, kind="handle", network="instagram", value="adacakes",
                                  token="t" * 32, status="verified"))
    db.session.commit()
    res = client.post(f"/api/directory/listings/{ada.id}/claim", json={"method": "handle"},
                      headers=auth_headers(vendor))
    assert res.get_json()["claim"]["status"] == "verified"
    assert _listing("Ada Cakes").status == "claimed"


# ── removal ────────────────────────────────────────────────────────────────

def test_public_removal_request_verified_by_handle(client, monkeypatch):
    _import(client)
    bola = _listing("Bola Prints")
    res = client.post(f"/api/directory/listings/{bola.id}/removal",
                      json={"method": "handle", "reason": "We closed this business"})
    assert res.status_code == 201
    claim = res.get_json()["claim"]
    row = DirectoryClaim.query.filter_by(public_id=claim["id"]).one()
    assert claim["instructions"]["bio"]["code"] == f"siiqo-{row.token[:8]}"

    monkeypatch.setattr("app.services.ownership._check_bio", lambda c: True)
    res = client.post(f"/api/directory/claims/{claim['id']}/check")
    assert res.get_json()["claim"]["status"] == "verified"
    bola = _listing("Bola Prints")
    assert bola.status == "removed" and bola.removed_reason == "owner_request"
    assert BusinessListingIdentifier.query.filter_by(listing_id=bola.id).count() == 0
    assert client.get(f"/api/directory/listings/{bola.id}").status_code == 404
    assert client.get("/api/check", query_string={"q": "@bolaprints"}).get_json().get("status") is None
    events = [e.event for e in DirectoryAuditEvent.query.filter_by(listing_id=bola.id).order_by(
        DirectoryAuditEvent.id)]
    assert events == ["DIRECTORY_REMOVAL_REQUESTED", "DIRECTORY_LISTING_REMOVED"]
    removed = DirectoryAuditEvent.query.filter_by(event="DIRECTORY_LISTING_REMOVED").one()
    assert removed.actor_type == "PUBLIC" and removed.details["reason"] == "We closed this business"


def test_removal_without_proof_changes_nothing(client, monkeypatch):
    _import(client)
    bola = _listing("Bola Prints")
    claim = client.post(f"/api/directory/listings/{bola.id}/removal",
                        json={"method": "handle"}).get_json()["claim"]
    monkeypatch.setattr("app.services.ownership._check_bio", lambda c: False)
    res = client.post(f"/api/directory/claims/{claim['id']}/check")
    assert res.get_json()["claim"]["status"] == "pending"
    assert _listing("Bola Prints").status == "unclaimed"
    # A method the listing doesn't have.
    res = client.post(f"/api/directory/listings/{bola.id}/removal", json={"method": "domain"})
    assert res.status_code == 400 and res.get_json()["code"] == "NO_WEBSITE"


# ── /start name check ──────────────────────────────────────────────────────

def test_name_check_returns_listing_match(client):
    _import(client)
    ada = _listing("Ada Cakes")
    data = client.get("/api/onboarding/name-check", query_string={"name": "ada cakes"}).get_json()
    assert data["listing_match"] == {"id": ada.id, "name": "Ada Cakes", "category": "Bakery", "city": "Wuse",
                                     "state": "Abuja"}
    data = client.get("/api/onboarding/name-check", query_string={"name": "Totally New Brand"}).get_json()
    assert data["listing_match"] is None
    ada.status = "claimed"
    db.session.commit()
    data = client.get("/api/onboarding/name-check", query_string={"name": "Ada Cakes"}).get_json()
    assert data["listing_match"] is None


# ── privacy ────────────────────────────────────────────────────────────────

PERSONAL_FIELD_HINTS = ("email", "owner", "first_name", "last_name", "full_name", "personal", "nin", "bvn",
                        "address", "dob", "birth", "gender", "id_number", "photo", "selfie")


@pytest.mark.parametrize("model", [BusinessListing, BusinessListingIdentifier])
def test_listing_tables_hold_no_personal_data_fields(model):
    columns = {c.name for c in model.__table__.columns}
    for col in columns:
        assert not any(hint in col for hint in PERSONAL_FIELD_HINTS), col
    if model is BusinessListing:
        assert {"phone", "website", "social_network", "social_handle"} <= columns


def test_imported_personal_columns_are_dropped(client):
    _import(client)
    ada = _listing("Ada Cakes")
    stored = str({c.name: getattr(ada, c.name) for c in BusinessListing.__table__.columns})
    assert "ada@gmail.com" not in stored and "Ada Obi" not in stored
    from app.models.directory import DirectoryImportBatch
    assert "ada@gmail.com" not in str(DirectoryImportBatch.query.one().rows)
