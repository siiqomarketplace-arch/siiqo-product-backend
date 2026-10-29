"""S3: the product scraper only fetches public http(s) addresses, re-checks
every redirect, verifies TLS, and the debug endpoint is gone."""
import socket

import pytest

from app.utils import scraper


def _resolve_to(monkeypatch, mapping):
    def fake(host, port, *a, **k):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (mapping[host], port))]
    monkeypatch.setattr(scraper.socket, "getaddrinfo", fake)


class _Resp:
    def __init__(self, status=200, location=None, text="<html><title>ok</title></html>"):
        self.status_code = status
        self.headers = {"location": location} if location else {}
        self.text = text
        self.is_redirect = location is not None

    def raise_for_status(self):
        pass


@pytest.fixture
def no_proxy(monkeypatch):
    monkeypatch.delenv("BRIGHT_DATA_PROXY", raising=False)


@pytest.mark.parametrize("url", [
    "http://169.254.169.254/latest/meta-data/",
    "http://127.0.0.1:5000/admin",
    "http://10.0.0.5/",
    "http://[::1]/",
    "file:///etc/passwd",
    "ftp://example.com/x",
])
def test_internal_and_non_http_urls_refused(url, monkeypatch, no_proxy):
    calls = []
    monkeypatch.setattr(scraper.requests, "get", lambda *a, **k: calls.append(a) or _Resp())
    result = scraper.scrape_product_url(url)
    assert result["success"] is False
    assert calls == []


def test_hostname_resolving_to_private_ip_refused(monkeypatch, no_proxy):
    _resolve_to(monkeypatch, {"evil.example": "192.168.1.10"})
    calls = []
    monkeypatch.setattr(scraper.requests, "get", lambda *a, **k: calls.append(a) or _Resp())
    assert scraper.scrape_product_url("https://evil.example/p")["success"] is False
    assert calls == []


def test_redirect_to_internal_address_refused(monkeypatch, no_proxy):
    _resolve_to(monkeypatch, {"shop.example": "93.184.216.34"})
    calls = []

    def fake_get(url, **kwargs):
        calls.append((url, kwargs))
        return _Resp(status=302, location="http://169.254.169.254/latest/meta-data/")
    monkeypatch.setattr(scraper.requests, "get", fake_get)
    assert scraper.scrape_product_url("https://shop.example/p")["success"] is False
    assert [c[0] for c in calls] == ["https://shop.example/p"]
    assert calls[0][1].get("verify", True) is True


def test_public_url_is_fetched(monkeypatch, no_proxy):
    _resolve_to(monkeypatch, {"shop.example": "93.184.216.34"})
    monkeypatch.setattr(scraper.requests, "get", lambda url, **k: _Resp())
    assert scraper._fetch_page_content("https://shop.example/p").startswith("<html>")


def test_debug_endpoint_removed(client, make_vendor, auth_headers):
    vendor, _ = make_vendor()
    res = client.post("/api/vendor/products/scrape-debug", json={"url": "https://x.example"},
                      headers=auth_headers(vendor))
    assert res.status_code in (404, 405)
