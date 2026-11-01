"""
Locale support for emails and bot / notification messages (D19).

English only for now (founder decision, 28 Oct 2026). This module is the
foundation that lets Nigerian Pidgin (pcm), Yoruba (yo), Igbo (ig) and Hausa
(ha) be added later by adding human-reviewed text, never machine translation
(docs/TRANSLATIONS.md):

- Emails: send_siiqo_email(..., locale=...) renders
  templates/emails/<locale>/<name>.html when that file exists, else the English
  templates/emails/<name>.html.
- Short messages (in-app notifications, bot text): message(key, locale, **params)
  reads MESSAGES[locale][key] and falls back to English per key.

A locale is used only when it is listed in LANGUAGES (env, comma separated,
default "en"). Anything else resolves to English.
"""
from __future__ import annotations

import logging
import os

from .messages import MESSAGES

logger = logging.getLogger(__name__)

SUPPORTED_LOCALES = ("en", "pcm", "yo", "ig", "ha")
DEFAULT_LOCALE = "en"


def normalize_locale(value) -> str | None:
    """'en-NG' -> 'en', 'YO' -> 'yo'; None for anything that is not a supported code."""
    if not isinstance(value, str):
        return None
    primary = value.strip().lower().replace("_", "-").split("-")[0]
    return primary if primary in SUPPORTED_LOCALES else None


def enabled_locales() -> tuple[str, ...]:
    """Locales switched on for this deploy. English is always first."""
    out = [DEFAULT_LOCALE]
    for part in (os.environ.get("LANGUAGES") or "").split(","):
        code = normalize_locale(part)
        if code and code not in out:
            out.append(code)
    return tuple(out)


def resolve_locale(value) -> str:
    """A usable locale: the given one if supported and enabled, else English."""
    code = normalize_locale(value)
    return code if code and code in enabled_locales() else DEFAULT_LOCALE


def user_locale(user) -> str:
    """The locale to write to this user in (their saved setting, else English)."""
    return resolve_locale(getattr(user, "preferred_locale", None) if user is not None else None)


def message(key: str, locale: str | None = None, **params) -> str:
    """Text for `key` in `locale`, falling back to English per key.

    A missing English key raises KeyError (a programming error, caught by tests).
    A translation that fails to format (wrong placeholder) falls back to English.
    """
    english = MESSAGES[DEFAULT_LOCALE][key]
    code = resolve_locale(locale)
    if code != DEFAULT_LOCALE:
        translated = (MESSAGES.get(code) or {}).get(key)
        if isinstance(translated, str) and translated.strip():
            try:
                return translated.format(**params)
            except (KeyError, IndexError, ValueError):
                logger.warning("[i18n] translation %s/%s failed to format; using English", code, key)
    return english.format(**params)


def email_template_names(template_name: str, locale: str | None = None) -> list[str]:
    """Templates to try, most specific first (Flask picks the first that exists)."""
    code = resolve_locale(locale)
    names = [f"emails/{template_name}.html"]
    if code != DEFAULT_LOCALE:
        names.insert(0, f"emails/{code}/{template_name}.html")
    return names
