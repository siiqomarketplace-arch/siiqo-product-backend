"""Siiqo no longer collects verification documents or stores typed NIN
numbers: verification runs through a provider (verification v2)."""
import io

from app.extensions import db
from app.models.user import Storefront, User


def _file():
    return (io.BytesIO(b"%PDF-1.4 fake"), "id.pdf")


def test_settings_ignores_documents_and_nin(client, make_vendor, auth_headers, monkeypatch):
    import app.routes.vendor as vendor_routes
    uploads = []
    monkeypatch.setattr(vendor_routes, "save_uploaded_file", lambda f, subfolder=None, **k: uploads.append(subfolder) or "https://x/f")
    vendor, store = make_vendor()
    res = client.patch("/api/vendor/update-settings", headers=auth_headers(vendor),
                       data={"nin": "12345678901", "nin_document": _file(), "cac_document": _file()},
                       content_type="multipart/form-data")
    assert res.status_code == 200, res.get_json()
    assert "verifications" not in uploads
    sf = db.session.get(Storefront, store.id)
    assert sf.nin_document_url is None and sf.cac_document_url is None
    assert db.session.get(User, vendor.id).nin in (None, "")


def test_own_settings_do_not_return_nin_or_document_links(client, make_vendor, auth_headers):
    vendor, store = make_vendor()
    vendor.nin = "12345678901"
    store.nin_document_url = "https://bucket/verifications/old.pdf"
    db.session.commit()
    body = client.get("/api/vendor/settings", headers=auth_headers(vendor)).get_data(as_text=True)
    assert "12345678901" not in body
    assert "old.pdf" not in body
