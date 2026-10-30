"""
totp.py — Time-based one-time passwords (RFC 6238) plus the at-rest
protection for admin 2FA material. Standard library only.

* TOTP: HMAC-SHA1, 6 digits, 30 second step, accepts +/-1 step of drift.
  verify() returns the matched time step so the caller can refuse a step that
  was already used (replay protection is stored per admin in the database).
* Secret encryption: `cryptography` is not a dependency of this project, so
  the secret is sealed with an encrypt-then-MAC construction built on
  HMAC-SHA256: an HMAC-SHA256 counter-mode keystream (a PRF used as a stream
  cipher, with a fresh 128-bit random nonce per seal) XORed with the
  plaintext, then an HMAC-SHA256 tag over version || nonce || ciphertext,
  checked in constant time before anything is decrypted. Encryption and MAC
  keys are derived separately from the server key (ADMIN_TOTP_ENC_KEY if set,
  otherwise SECRET_KEY). Rotating that key makes stored secrets unreadable,
  so affected admins must re-enrol.
* Recovery codes: random, shown once, stored only as HMAC-SHA256 digests keyed
  with a derived pepper, so a database dump alone cannot be brute-forced.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import os
import secrets
import struct
import time
from urllib.parse import quote, urlencode

DIGITS = 6
PERIOD = 30
WINDOW = 1  # accept the previous and next step as well
SECRET_BYTES = 20  # 160 bits, the RFC 4226 recommendation for SHA1
RECOVERY_CODE_COUNT = 10

_ENC_VERSION = "v1"
_NONCE_BYTES = 16
_TAG_BYTES = 32


# ---------------------------------------------------------------------------
# TOTP core (RFC 4226 / RFC 6238)
# ---------------------------------------------------------------------------

def generate_secret() -> str:
    """A new random base32 secret (no padding), as authenticator apps expect."""
    return base64.b32encode(secrets.token_bytes(SECRET_BYTES)).decode("ascii").rstrip("=")


def _b32decode(secret_b32: str) -> bytes:
    cleaned = secret_b32.replace(" ", "").upper()
    cleaned += "=" * (-len(cleaned) % 8)
    return base64.b32decode(cleaned, casefold=True)


def hotp(key: bytes, counter: int, digits: int = DIGITS, digest=hashlib.sha1) -> str:
    """RFC 4226 HOTP value for a raw key and counter."""
    mac = hmac.new(key, struct.pack(">Q", counter), digest).digest()
    offset = mac[-1] & 0x0F
    binary = struct.unpack(">I", mac[offset:offset + 4])[0] & 0x7FFFFFFF
    return str(binary % (10 ** digits)).zfill(digits)


def time_step(for_time: float | None = None, period: int = PERIOD) -> int:
    return int((time.time() if for_time is None else for_time) // period)


def totp(secret_b32: str, for_time: float | None = None, digits: int = DIGITS,
         period: int = PERIOD) -> str:
    """Current TOTP code for a base32 secret."""
    return hotp(_b32decode(secret_b32), time_step(for_time, period), digits)


def verify(secret_b32: str, code: str, for_time: float | None = None,
           window: int = WINDOW, last_used_step: int | None = None) -> int | None:
    """
    Check a TOTP code. Returns the matched time step, or None.

    Steps at or before `last_used_step` are never accepted, which is what
    stops the same code (or an older one) being replayed.
    """
    code = (code or "").strip().replace(" ", "")
    if len(code) != DIGITS or not code.isdigit():
        return None
    try:
        key = _b32decode(secret_b32)
    except Exception:
        return None
    now_step = time_step(for_time)
    matched = None
    # Check every step in the window (no early exit) to keep timing uniform.
    for step in range(now_step - window, now_step + window + 1):
        if step < 0:
            continue
        if hmac.compare_digest(hotp(key, step), code):
            if last_used_step is None or step > last_used_step:
                matched = step if matched is None else max(matched, step)
    return matched


def provisioning_uri(secret_b32: str, account_name: str, issuer: str = "Siiqo Admin") -> str:
    """otpauth:// URI for QR codes (Google Authenticator key-uri format)."""
    label = quote(f"{issuer}:{account_name}", safe="@")
    params = urlencode({
        "secret": secret_b32,
        "issuer": issuer,
        "algorithm": "SHA1",
        "digits": DIGITS,
        "period": PERIOD,
    }, quote_via=quote)
    return f"otpauth://totp/{label}?{params}"


# ---------------------------------------------------------------------------
# Key derivation
# ---------------------------------------------------------------------------

def _master_key() -> bytes:
    key = os.getenv("ADMIN_TOTP_ENC_KEY")
    if not key:
        try:
            from flask import current_app
            key = current_app.config.get("SECRET_KEY")
        except RuntimeError:
            key = None
    if not key:
        key = os.getenv("SECRET_KEY")
    if not key:
        raise RuntimeError("No SECRET_KEY / ADMIN_TOTP_ENC_KEY configured for 2FA secret encryption")
    return key.encode("utf-8") if isinstance(key, str) else bytes(key)


def _derive(label: str) -> bytes:
    return hmac.new(_master_key(), f"siiqo-admin-2fa/{label}".encode("ascii"), hashlib.sha256).digest()


# ---------------------------------------------------------------------------
# Authenticated encryption of the TOTP secret
# ---------------------------------------------------------------------------

def _keystream(key: bytes, nonce: bytes, length: int) -> bytes:
    out = bytearray()
    counter = 0
    while len(out) < length:
        out += hmac.new(key, nonce + struct.pack(">Q", counter), hashlib.sha256).digest()
        counter += 1
    return bytes(out[:length])


def encrypt_secret(plaintext: str) -> str:
    enc_key, mac_key = _derive("enc-v1"), _derive("mac-v1")
    nonce = secrets.token_bytes(_NONCE_BYTES)
    data = plaintext.encode("utf-8")
    ct = bytes(a ^ b for a, b in zip(data, _keystream(enc_key, nonce, len(data))))
    tag = hmac.new(mac_key, _ENC_VERSION.encode() + nonce + ct, hashlib.sha256).digest()
    blob = base64.urlsafe_b64encode(nonce + ct + tag).decode("ascii")
    return f"{_ENC_VERSION}:{blob}"


def decrypt_secret(token: str | None) -> str | None:
    """Returns the plaintext, or None if missing, tampered with or sealed under another key."""
    if not token or not token.startswith(f"{_ENC_VERSION}:"):
        return None
    try:
        raw = base64.urlsafe_b64decode(token.split(":", 1)[1].encode("ascii"))
    except Exception:
        return None
    if len(raw) < _NONCE_BYTES + _TAG_BYTES:
        return None
    nonce, ct, tag = raw[:_NONCE_BYTES], raw[_NONCE_BYTES:-_TAG_BYTES], raw[-_TAG_BYTES:]
    enc_key, mac_key = _derive("enc-v1"), _derive("mac-v1")
    expected = hmac.new(mac_key, _ENC_VERSION.encode() + nonce + ct, hashlib.sha256).digest()
    if not hmac.compare_digest(expected, tag):
        return None
    pt = bytes(a ^ b for a, b in zip(ct, _keystream(enc_key, nonce, len(ct))))
    try:
        return pt.decode("utf-8")
    except UnicodeDecodeError:
        return None


# ---------------------------------------------------------------------------
# Recovery codes
# ---------------------------------------------------------------------------

_RECOVERY_ALPHABET = "abcdefghjkmnpqrstuvwxyz23456789"  # no 0/o/1/l/i


def normalize_recovery_code(code: str) -> str:
    return "".join(ch for ch in (code or "").lower() if ch.isalnum())


def hash_recovery_code(code: str) -> str:
    return hmac.new(_derive("recovery-v1"), normalize_recovery_code(code).encode("ascii", "ignore"),
                    hashlib.sha256).hexdigest()


def generate_recovery_codes(count: int = RECOVERY_CODE_COUNT) -> list[str]:
    """Codes like 'k7m2p-9xq4t' (~50 bits each)."""
    codes = []
    for _ in range(count):
        chars = "".join(secrets.choice(_RECOVERY_ALPHABET) for _ in range(10))
        codes.append(f"{chars[:5]}-{chars[5:]}")
    return codes


def match_recovery_code(code: str, hashes: list[str] | None) -> int | None:
    """Index of the matching stored hash, or None. Compares against every hash."""
    if not hashes or len(normalize_recovery_code(code)) != 10:
        return None
    candidate = hash_recovery_code(code)
    found = None
    for i, stored in enumerate(hashes):
        if isinstance(stored, str) and hmac.compare_digest(stored, candidate):
            found = i
    return found
