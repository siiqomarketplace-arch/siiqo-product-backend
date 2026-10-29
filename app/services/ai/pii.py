"""
Strip personal data before anything is sent to an AI provider.

Policy (AI_ORCHESTRATION_OPTIONS.md Q9): never send NIN, BVN, bank account
numbers, card numbers, buyer contact details or ID images. This module masks
the text forms of those. It is deliberately over-eager: a masked product code
is a small loss, a leaked BVN is not.

Order matters - more specific patterns run first so that, for example, a
16-digit card is not half-masked as an account number.
"""
from __future__ import annotations

import re
from typing import Any

EMAIL = "[email]"
CARD = "[card_number]"
PHONE = "[phone]"
ID_NUMBER = "[id_number]"
ACCOUNT = "[account_number]"
REDACTED = "[redacted]"

# No digit immediately before / after, so "acct:0123456789" still matches but
# a 12-digit run is not mistaken for a 10-digit account number.
_NB = r"(?<!\d)"
_NA = r"(?!\d)"

_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")

# 13-19 digits, optionally grouped with single spaces or dashes (card PANs).
_CARD_RE = re.compile(_NB + r"\d(?:[ \-]?\d){12,18}" + _NA)

# Nigerian mobiles: +234 / 234 / 0, then 7/8/9, then 0/1, then 8 digits.
# Separators (space, dash, dot) allowed between groups; "(0)" after +234 tolerated.
_PHONE_RE = re.compile(
    _NB + r"(?:\+?234[ \-.]?(?:\(0\)[ \-.]?)?|0)[789][01](?:[ \-.]?\d){8}" + _NA
)

# 11-digit NIN / BVN-like identifiers.
_ID11_RE = re.compile(_NB + r"\d{11}" + _NA)

# 10-digit NUBAN bank account numbers.
_ACCOUNT_RE = re.compile(_NB + r"\d{10}" + _NA)

_PATTERNS: list[tuple[re.Pattern, str]] = [
    (_EMAIL_RE, EMAIL),
    # Phones before cards: "+234 803 123 4567" is 13 digits and would
    # otherwise look card-like.
    (_PHONE_RE, PHONE),
    (_CARD_RE, CARD),
    (_ID11_RE, ID_NUMBER),
    (_ACCOUNT_RE, ACCOUNT),
]

# Dict keys whose values are dropped wholesale, whatever they contain.
_SENSITIVE_KEY_RE = re.compile(
    r"(^|_)(bvn|nin|ssn|password|passcode|pin|otp|token|secret|account_?(number|no)|acct|iban|"
    r"card|cvv|pan|phone|mobile|msisdn|email|dob|date_of_birth)($|_)",
    re.IGNORECASE,
)


def strip_text(text: str) -> tuple[str, int]:
    """Mask personal data in one string. Returns (clean_text, number_of_redactions)."""
    if not text:
        return text, 0
    total = 0
    for pattern, replacement in _PATTERNS:
        text, n = pattern.subn(replacement, text)
        total += n
    return text, total


def _is_sensitive_key(key: Any) -> bool:
    return isinstance(key, str) and bool(_SENSITIVE_KEY_RE.search(key))


def strip(value: Any) -> tuple[Any, int]:
    """
    Recursively mask personal data in str / dict / list / tuple values.
    Values under sensitive-looking keys (``phone``, ``bvn``, ``account_number``...)
    are replaced with ``[redacted]``. Returns (clean_value, number_of_redactions).
    """
    if isinstance(value, str):
        return strip_text(value)
    if isinstance(value, dict):
        out, total = {}, 0
        for k, v in value.items():
            if _is_sensitive_key(k) and v not in (None, "", [], {}):
                out[k] = REDACTED
                total += 1
                continue
            out[k], n = strip(v)
            total += n
        return out, total
    if isinstance(value, (list, tuple)):
        items, total = [], 0
        for v in value:
            clean, n = strip(v)
            items.append(clean)
            total += n
        return (items if isinstance(value, list) else tuple(items)), total
    return value, 0
