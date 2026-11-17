"""
Client IP resolution behind Siiqo's proxy chain.

Used as the rate-limiter key and as request.remote_addr (see ClientIPMiddleware),
so limits, anomaly detection, audit logs and pay-request limits all agree.

Real hop chains (as seen by Flask, left = oldest):

  Website traffic (browser -> siiqo.com -> Next.js rewrite /api/* on Amplify):
    [client-set junk...], VIEWER, CLOUDFRONT_EDGE, AMPLIFY_HOST, ALB_PRIVATE   (socket: nginx 127.0.0.1)
    e.g. "102.90.118.132, 141.0.12.97, 64.252.66.39, 3.82.120.145, 172.31.x.x"
      - CloudFront appends the viewer it saw (141.0.12.97)
      - the Amplify SSR server appends its peer, a CloudFront edge (64.252.66.39)
      - the ALB appends the Amplify host that connected to it (3.82.120.145)
      - nginx appends the ALB's private address
  Direct traffic (bot server, uptime checks, old app builds -> api.siiqo.app):
    [client-set junk...], CLIENT, ALB_PRIVATE

Rule (walk X-Forwarded-For from the RIGHT, socket address included):
  1. Skip hops that are trusted proxies: private/loopback/link-local (ALB, nginx),
     AWS CloudFront ranges (vendored snapshot, cloudfront_ip_ranges.json), and any
     CIDR in env TRUSTED_PROXY_CIDRS (comma-separated).
  2. The first untrusted hop is the client - EXCEPT once: if the hop directly to its
     left is a CloudFront edge, that untrusted hop is the website's SSR host
     (AWS compute, not pinnable to a small range), so skip it too and continue.
     If env WEBSITE_SERVER_CIDRS is set, this exception only applies when the hop
     is inside those CIDRs.
  3. CF-Connecting-IP / True-Client-IP are ignored unless the first untrusted hop
     is inside env CLOUDFLARE_TRUSTED (comma-separated Cloudflare CIDRs; default
     empty = never honoured).
  Entries left of the chosen hop are client-controlled and never used.

Known limit of rule 2: someone who connects to the ALB directly and forges
"x, <CloudFront IP>" in X-Forwarded-For gets keyed on x. Setting
WEBSITE_SERVER_CIDRS (or locking the ALB to CloudFront / a shared header) closes it.
"""
from __future__ import annotations

import ipaddress
import json
import os
from functools import lru_cache
from pathlib import Path

_RANGES_FILE = Path(__file__).with_name("cloudfront_ip_ranges.json")

_PRIVATE_CIDRS = (
    "10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "127.0.0.0/8",
    "169.254.0.0/16", "100.64.0.0/10", "::1/128", "fc00::/7", "fe80::/10",
)


def _parse_cidrs(raw) -> tuple:
    nets = []
    for item in raw:
        item = (item or "").strip()
        if not item:
            continue
        try:
            nets.append(ipaddress.ip_network(item, strict=False))
        except ValueError:
            continue
    return tuple(nets)


def _env_cidrs(name: str) -> tuple:
    return _parse_cidrs((os.environ.get(name) or "").split(","))


@lru_cache(maxsize=1)
def _cloudfront_nets() -> tuple:
    try:
        data = json.loads(_RANGES_FILE.read_text(encoding="utf-8"))
    except Exception:
        return ()
    return _parse_cidrs(list(data.get("CLOUDFRONT_GLOBAL_IP_LIST", []))
                        + list(data.get("CLOUDFRONT_REGIONAL_EDGE_IP_LIST", [])))


@lru_cache(maxsize=1)
def _config() -> dict:
    return {
        "private": _parse_cidrs(_PRIVATE_CIDRS),
        "extra": _env_cidrs("TRUSTED_PROXY_CIDRS"),
        "cloudfront": _cloudfront_nets(),
        "website": _env_cidrs("WEBSITE_SERVER_CIDRS"),
        "cloudflare": _env_cidrs("CLOUDFLARE_TRUSTED"),
    }


def reload_config() -> None:
    """Re-read env vars (tests / after config change)."""
    _config.cache_clear()


def _ip(value):
    try:
        addr = ipaddress.ip_address((value or "").strip().strip("[]"))
    except ValueError:
        return None
    if addr.version == 6 and addr.ipv4_mapped:
        addr = addr.ipv4_mapped
    return addr


def _in(addr, nets) -> bool:
    return any(addr.version == n.version and addr in n for n in nets)


def _trusted(addr, cfg) -> bool:
    return _in(addr, cfg["private"]) or _in(addr, cfg["cloudfront"]) or _in(addr, cfg["extra"])


def resolve_client_ip(remote_addr: str | None, xff: str | None,
                      cf_connecting_ip: str | None = None,
                      true_client_ip: str | None = None) -> str:
    cfg = _config()
    hops = [h.strip() for h in (xff or "").split(",") if h.strip()]
    if remote_addr:
        hops.append(remote_addr)
    if not hops:
        return "127.0.0.1"

    website_hop_skipped = False
    last_good = None
    i = len(hops) - 1
    while i >= 0:
        addr = _ip(hops[i])
        if addr is None:
            # Garbage can only come from the client; stop at the last real hop.
            break
        last_good = addr
        if _trusted(addr, cfg):
            i -= 1
            continue
        # Untrusted hop.
        if cfg["cloudflare"] and _in(addr, cfg["cloudflare"]):
            for header in (cf_connecting_ip, true_client_ip):
                cf_addr = _ip(header)
                if cf_addr is not None:
                    return str(cf_addr)
        if not website_hop_skipped and i > 0:
            left = _ip(hops[i - 1])
            if (left is not None and _in(left, cfg["cloudfront"])
                    and (not cfg["website"] or _in(addr, cfg["website"]))):
                website_hop_skipped = True
                i -= 1
                continue
        return str(addr)
    return str(last_good) if last_good is not None else "127.0.0.1"


def client_ip_from_environ(environ) -> str:
    cached = environ.get("siiqo.client_ip")
    if cached:
        return cached
    orig = environ.get("siiqo.orig_remote_addr") or environ.get("REMOTE_ADDR")
    return resolve_client_ip(orig, environ.get("HTTP_X_FORWARDED_FOR"),
                             environ.get("HTTP_CF_CONNECTING_IP"),
                             environ.get("HTTP_TRUE_CLIENT_IP"))


class ClientIPMiddleware:
    """WSGI middleware: sets REMOTE_ADDR to the resolved client IP (replaces
    ProxyFix x_for) and keeps the socket peer in environ['siiqo.orig_remote_addr']."""

    def __init__(self, app):
        self.app = app

    def __call__(self, environ, start_response):
        orig = environ.get("REMOTE_ADDR")
        environ["siiqo.orig_remote_addr"] = orig
        try:
            ip = resolve_client_ip(orig, environ.get("HTTP_X_FORWARDED_FOR"),
                                   environ.get("HTTP_CF_CONNECTING_IP"),
                                   environ.get("HTTP_TRUE_CLIENT_IP"))
        except Exception:
            ip = orig or "127.0.0.1"
        environ["siiqo.client_ip"] = ip
        environ["REMOTE_ADDR"] = ip
        return self.app(environ, start_response)
