"""
Mask personal data for LOG TEXT only (never for what is stored or sent).

    mask_email("webmaster@gmail.com") -> "we***@gmail.com"
    mask_phone("+2348031234567")      -> "***4567"
    mask_account("0123456789")        -> "******6789"
"""
from __future__ import annotations

import re


def mask_email(value) -> str:
    if not value or not isinstance(value, str):
        return "***"
    value = value.strip()
    local, sep, domain = value.rpartition("@")
    if not sep or not local or not domain:
        return "***"
    keep = local[:2] if len(local) > 2 else local[:1]
    return f"{keep}***@{domain}"


_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")


def mask_emails_in(text) -> str:
    """Mask every email address inside free text (e.g. an exception message)."""
    return _EMAIL_RE.sub(lambda m: mask_email(m.group(0)), str(text))


def mask_phone(value) -> str:
    digits = re.sub(r"\D", "", str(value or ""))
    if len(digits) < 7:
        return "***"
    return f"***{digits[-4:]}"


def mask_account(value) -> str:
    digits = re.sub(r"\D", "", str(value or ""))
    if len(digits) < 6:
        return "***"
    return "*" * (len(digits) - 4) + digits[-4:]
