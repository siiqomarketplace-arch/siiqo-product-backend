"""Client IP resolution: spoofed headers must not change the rate-limit key."""
import pytest

from app.utils import client_ip as cip

ALB = "172.31.20.5"     # ALB private address appended by nginx
NGINX = "127.0.0.1"     # socket peer seen by gunicorn
REAL_WEBSITE_XFF = "102.90.118.132, 141.0.12.97, 64.252.66.39, 3.82.120.145"


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for k in ("TRUSTED_PROXY_CIDRS", "CLOUDFLARE_TRUSTED", "WEBSITE_SERVER_CIDRS"):
        monkeypatch.delenv(k, raising=False)
    cip.reload_config()
    yield
    cip.reload_config()


def r(xff, cf=None, tci=None, remote=NGINX):
    return cip.resolve_client_ip(remote, xff, cf, tci)


def test_real_website_chain_keys_on_cloudfront_viewer():
    assert r(f"{REAL_WEBSITE_XFF}, {ALB}") == "141.0.12.97"
    # also when nginx does not append the ALB address
    assert r(REAL_WEBSITE_XFF) == "141.0.12.97"


def test_direct_single_ip():
    assert r(f"198.51.100.7, {ALB}") == "198.51.100.7"
    assert r("198.51.100.7") == "198.51.100.7"
    assert r(None, remote="198.51.100.7") == "198.51.100.7"


@pytest.mark.parametrize("fake", ["1.2.3.4", "8.8.8.8", "10.0.0.9", "13.32.0.1", "not-an-ip"])
def test_spoofed_headers_do_not_change_key(fake):
    # Website path: extra left-most XFF entries
    assert r(f"{fake}, {REAL_WEBSITE_XFF}, {ALB}", cf=fake, tci=fake) == "141.0.12.97"
    # Direct path
    assert r(f"198.51.100.7, {ALB}", cf=fake, tci=fake) == "198.51.100.7"
    if fake != "13.32.0.1":
        assert r(f"{fake}, 198.51.100.7, {ALB}", cf=fake, tci=fake) == "198.51.100.7"


def test_known_limit_forged_cloudfront_hop_on_direct_path():
    # Documented limit: without WEBSITE_SERVER_CIDRS a direct caller that forges
    # "<x>, <CloudFront IP>" looks like the website's SSR hop. See client_ip.py.
    assert r(f"9.9.9.9, 13.32.0.1, 203.0.113.50, {ALB}") == "9.9.9.9"


def test_website_hop_exception_applies_once_only():
    # A client forging "<x>, <CloudFront IP>" through the website still keys on
    # the viewer CloudFront saw, because the SSR-hop exception is already used.
    xff = f"6.6.6.6, 13.32.0.1, 141.0.12.97, 64.252.66.39, 3.82.120.145, {ALB}"
    assert r(xff) == "141.0.12.97"


def test_website_server_cidrs_pin(monkeypatch):
    monkeypatch.setenv("WEBSITE_SERVER_CIDRS", "3.82.0.0/16")
    cip.reload_config()
    assert r(f"{REAL_WEBSITE_XFF}, {ALB}") == "141.0.12.97"
    # direct caller forging a CloudFront hop is now keyed on its own address
    assert r(f"9.9.9.9, 13.32.0.1, 203.0.113.50, {ALB}") == "203.0.113.50"


def test_trusted_proxy_cidrs_env(monkeypatch):
    monkeypatch.setenv("TRUSTED_PROXY_CIDRS", "203.0.113.0/24, garbage")
    cip.reload_config()
    assert r(f"198.51.100.7, 203.0.113.9, {ALB}") == "198.51.100.7"


def test_cloudflare_headers_only_from_listed_cloudflare_hop(monkeypatch):
    assert r(f"173.245.48.10, {ALB}", cf="5.5.5.5") == "173.245.48.10"
    monkeypatch.setenv("CLOUDFLARE_TRUSTED", "173.245.48.0/20")
    cip.reload_config()
    assert r(f"173.245.48.10, {ALB}", cf="5.5.5.5") == "5.5.5.5"
    assert r(f"173.245.48.10, {ALB}", tci="5.5.5.6") == "5.5.5.6"
    # a non-Cloudflare hop still can't use the header
    assert r(f"198.51.100.7, {ALB}", cf="5.5.5.5") == "198.51.100.7"


def test_request_remote_addr_matches_limiter_key(app):
    from flask import request
    from werkzeug.test import EnvironBuilder
    from app.extensions import get_real_client_ip
    seen = {}

    def inner(environ, start_response):
        with app.request_context(environ):
            seen["remote"] = request.remote_addr
            seen["key"] = get_real_client_ip()
        start_response("200 OK", [])
        return [b"ok"]

    env = EnvironBuilder(path="/", headers={
        "X-Forwarded-For": f"1.1.1.1, {REAL_WEBSITE_XFF}, {ALB}",
        "CF-Connecting-IP": "2.2.2.2", "True-Client-IP": "3.3.3.3"},
        environ_base={"REMOTE_ADDR": NGINX}).get_environ()
    cip.ClientIPMiddleware(inner)(env, lambda *a: None)
    assert seen == {"remote": "141.0.12.97", "key": "141.0.12.97"}
    assert env["siiqo.orig_remote_addr"] == NGINX


def test_app_is_wrapped_with_client_ip_middleware(app):
    assert isinstance(app.wsgi_app, cip.ClientIPMiddleware)


def test_login_limit_holds_against_rotating_fake_xff(client):
    from app.extensions import limiter
    limiter.enabled = True
    try:
        limiter.reset()
        codes = []
        for i in range(12):
            res = client.post("/api/auth/login",
                              json={"email": "nobody@example.com", "password": "x"},
                              headers={"X-Forwarded-For": f"9.9.{i}.{i}, 198.51.100.77, {ALB}",
                                       "CF-Connecting-IP": f"7.7.{i}.{i}",
                                       "True-Client-IP": f"6.6.{i}.{i}"})
            codes.append(res.status_code)
        assert 429 in codes
        assert codes.index(429) == 10
    finally:
        limiter.reset()
        limiter.enabled = False
