"""
NIPOST postcode client (https://api.postcode.gov.ng, docs read 18 Nov 2026).

Only FREE endpoints are used:
  GET /v1/lookup?code=<code>&level=1        Validator: {postcode, valid}
  GET /v1/assembly/disassemble?code=<code>  {state, lga, district, area, unit}
  GET /v1/reference/states                  {states: [{code, name}]}
  GET /v1/reference/lgas?state=<code>       {lgas: [{code, name}]}

Never level 3 (Autofill, paid) and never a fallback to a paid level.
Lookup results are not cached (NIPOST AUP); only the public reference
lists (state and LGA names) are kept in memory, for at most 24 hours.

Errors (stable codes for the website):
  POSTCODE_INVALID      400  not a real postcode
  POSTCODE_BUSY         503  NIPOST rate-limited us twice in a row
  POSTCODE_UNAVAILABLE  503  key rejected (401), no credits (402), level or
                             origin not granted (403), network/5xx, no key

Env: NIPOST_API_KEY (secret, server only; never logged), NIPOST_API_BASE
(default https://api.postcode.gov.ng), NIPOST_ENV (test|live; derived from
the key prefix when unset).
"""
from __future__ import annotations

import logging
import os
import re
import threading
import time
from dataclasses import dataclass

import requests

logger = logging.getLogger(__name__)

DEFAULT_BASE = "https://api.postcode.gov.ng"
TIMEOUT = 5  # seconds, per request
RETRY_BACKOFF_SECONDS = 1.5  # one short wait after a 429, then give up
REFERENCE_TTL = 24 * 3600  # AUP: reference names kept in memory at most 24 h

# Compact postcode: state(2) lga(2) district(3) area(2) unit(2), e.g. FC01A01KP27.
_COMPACT = re.compile(r"^[A-Z]{2}[0-9]{2}[A-Z0-9]{3}[A-Z]{2}[0-9]{2}$")

# Sandbox postcodes that work with test keys (docs.postcode.gov.ng).
SANDBOX_POSTCODES = ("FC-01-A01-KP-27", "FC-01-A01-LR-01", "FC-01-A01-MH-01",
                     "FC-01-A01-MV-01", "FC-01-A01-MW-01")


class PostcodeError(Exception):
    def __init__(self, code: str, message: str, status: int):
        super().__init__(message)
        self.code, self.message, self.status = code, message, status


def invalid():
    return PostcodeError("POSTCODE_INVALID",
                         "That postcode isn't right. Check it and try again, or pick your shop on the map.", 400)


def busy():
    return PostcodeError("POSTCODE_BUSY", "NIPOST is busy. Try again in a minute.", 503)


def unavailable():
    return PostcodeError("POSTCODE_UNAVAILABLE",
                         "We can't check postcodes right now. Please try again later.", 503)


@dataclass
class PostcodeResult:
    postcode: str          # compact, upper
    display: str           # dashed
    state_code: str | None
    lga_code: str | None
    district_code: str | None
    area_code: str | None
    unit_code: str | None
    state_name: str | None
    lga_name: str | None


# ── format ──────────────────────────────────────────────────────────────────

def normalise(code) -> str | None:
    """'fc-01-a01-kp-27' / 'FC01A01KP27' / 'FC 01 A01 KP 27' -> 'FC01A01KP27', or None."""
    if not isinstance(code, str):
        return None
    compact = re.sub(r"[^A-Za-z0-9]", "", code).upper()
    return compact if _COMPACT.match(compact) else None


def dashed(compact: str) -> str:
    c = compact
    return f"{c[0:2]}-{c[2:4]}-{c[4:7]}-{c[7:9]}-{c[9:11]}"


# ── config ──────────────────────────────────────────────────────────────────

def _key() -> str:
    return (os.environ.get("NIPOST_API_KEY") or "").strip()


def _base() -> str:
    return (os.environ.get("NIPOST_API_BASE") or DEFAULT_BASE).strip().rstrip("/")


def nipost_env() -> str:
    """'test' or 'live'. NIPOST_ENV wins; otherwise from the key prefix."""
    env = (os.environ.get("NIPOST_ENV") or "").strip().lower()
    if env in ("test", "live"):
        return env
    return "live" if _key().startswith("nipost_live_") else "test"


def is_configured() -> bool:
    return bool(_key())


# ── HTTP ────────────────────────────────────────────────────────────────────

def _error_code(resp) -> str:
    try:
        return str(((resp.json() or {}).get("error") or {}).get("code") or "")
    except Exception:
        return ""


def _get(path: str, params: dict | None = None) -> dict:
    """GET a NIPOST endpoint. Retries once after a 429. Raises PostcodeError.
    The key goes only in the X-API-Key header and is never logged."""
    key = _key()
    if not key:
        logger.error("[NIPOST] NIPOST_API_KEY is not set; postcode checks are unavailable")
        raise unavailable()
    url = f"{_base()}{path}"
    headers = {"X-API-Key": key, "Accept": "application/json"}
    for attempt in (1, 2):
        try:
            resp = requests.get(url, params=params, headers=headers, timeout=TIMEOUT)
        except requests.RequestException as exc:
            logger.warning("[NIPOST] %s network error: %s", path, type(exc).__name__)
            raise unavailable()
        status = resp.status_code
        if status == 429:
            if attempt == 1:
                time.sleep(RETRY_BACKOFF_SECONDS)
                continue
            logger.warning("[NIPOST] %s rate limited twice (remaining=%s)", path,
                           resp.headers.get("X-RateLimit-Remaining"))
            raise busy()
        if status == 401:
            logger.error("[NIPOST] key rejected (401) on %s; check NIPOST_API_KEY", path)
            raise unavailable()
        if status in (402, 403):
            # ADMIN ALERT: logged at ERROR so monitoring (Sentry) raises it.
            # Never retried at another (paid) level.
            logger.error("[NIPOST] ALERT %s on %s (code=%s): postcode checks are unavailable",
                         status, path, _error_code(resp) or "-")
            raise unavailable()
        if status in (400, 404, 422):
            raise invalid()
        if status >= 400:
            logger.warning("[NIPOST] %s answered %s", path, status)
            raise unavailable()
        try:
            body = resp.json()
        except ValueError:
            logger.warning("[NIPOST] %s answered non-JSON", path)
            raise unavailable()
        if not isinstance(body, dict):
            raise unavailable()
        return body


# ── reference names (free, public; cached in memory ≤ 24 h) ─────────────────

_ref_lock = threading.Lock()
_ref_cache: dict[str, tuple[float, dict]] = {}


def clear_reference_cache():
    with _ref_lock:
        _ref_cache.clear()


def _reference(cache_key: str, path: str, params: dict | None, list_key: str) -> dict:
    now = time.time()
    with _ref_lock:
        hit = _ref_cache.get(cache_key)
        if hit and now - hit[0] < REFERENCE_TTL:
            return hit[1]
    body = _get(path, params)
    names = {}
    for item in body.get(list_key) or []:
        if isinstance(item, dict) and item.get("code") and item.get("name"):
            names[str(item["code"]).upper()] = str(item["name"]).strip()[:120]
    with _ref_lock:
        _ref_cache[cache_key] = (now, names)
    return names


def state_name(state_code: str | None) -> str | None:
    if not state_code:
        return None
    return _reference("states", "/v1/reference/states", None, "states").get(state_code.upper())


def lga_name(state_code: str | None, lga_code: str | None) -> str | None:
    if not state_code or not lga_code:
        return None
    return _reference(f"lgas:{state_code.upper()}", "/v1/reference/lgas",
                      {"state": state_code.upper()}, "lgas").get(lga_code.upper())


# ── validate ────────────────────────────────────────────────────────────────

def _segment(value) -> str | None:
    if isinstance(value, dict):
        value = value.get("code")
    if value is None or value == "":
        return None
    return str(value).strip().upper()[:6]


def validate(code) -> PostcodeResult:
    """Validator (level 1, free) then disassemble (free), then names from the
    cached reference lists. Raises PostcodeError. Nothing is cached or stored here."""
    compact = normalise(code)
    if compact is None:
        raise invalid()

    found = _get("/v1/lookup", {"code": compact, "level": 1})
    if found.get("valid") is not True:
        raise invalid()

    parts = _get("/v1/assembly/disassemble", {"code": compact})
    state = _segment(parts.get("state")) or compact[0:2]
    lga = _segment(parts.get("lga")) or compact[2:4]
    district = _segment(parts.get("district")) or compact[4:7]
    area = _segment(parts.get("area")) or compact[7:9]
    unit = _segment(parts.get("unit")) or compact[9:11]

    # Names are labels only; a failure here must not undo a valid postcode.
    s_name = l_name = None
    try:
        s_name = state_name(state)
        l_name = lga_name(state, lga)
    except PostcodeError:
        logger.info("[NIPOST] reference names unavailable; saving codes only")

    return PostcodeResult(postcode=compact, display=dashed(compact), state_code=state, lga_code=lga,
                          district_code=district, area_code=area, unit_code=unit,
                          state_name=s_name, lga_name=l_name)


def short_state(name: str | None) -> str | None:
    if not name:
        return None
    if "federal capital" in name.lower():
        return "FCT"
    return name


def area_label(state_name_: str | None, lga_name_: str | None, state_code: str | None = None,
               lga_code: str | None = None) -> str:
    """'AMAC, FCT'. Falls back to the codes when NIPOST gave no names."""
    names = [n for n in (lga_name_, short_state(state_name_)) if n]
    if names:
        return ", ".join(names)
    if state_code and lga_code:
        return f"{state_code}-{lga_code}"
    return state_code or ""
