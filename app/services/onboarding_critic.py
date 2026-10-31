"""
Code critic for AI onboarding drafts (Phase 1). No model involved.

``check_spec`` and ``check_listing`` return a list of problems (empty = pass):

  * schema: the Store Spec rules from ``store_spec.parse_store_spec``
    (version 1, the 14 presets, section ids, max lengths, hex colours);
  * required sections and a non-empty headline;
  * banned claims: never allowed (verified, escrow, guaranteed, genuine,
    warranty, fees, ...), plus "claim words" allowed only when the seller's
    own answers use them (authentic, organic, best, same-day, ...);
  * fact trace: every number (prices, years, counts) and every place name
    in the draft must appear in the seller's answers.
"""
from __future__ import annotations

import re
from typing import Any, Iterable

from app.services.store_spec import parse_store_spec, text_fields

REQUIRED_SECTIONS = ("hero", "products", "contact")

# Never written by the AI, whatever the seller said (PHASE_1_PLAN rules).
HARD_BANNED: tuple[tuple[str, re.Pattern], ...] = tuple(
    (label, re.compile(pattern, re.IGNORECASE)) for label, pattern in (
        ("verified", r"\bverif(?:y|ied|ies|ication)\b"),
        ("escrow", r"\bescrow\b"),
        ("buyer protection", r"\bbuyer[\s-]+protection\b|\bprotected\s+(?:payment|purchase)s?\b"),
        ("guaranteed", r"\bguarant(?:ee|eed|ees|eeing)\b"),
        ("genuine", r"\bgenuine\b"),
        ("warranty", r"\bwarrant(?:y|ies|ied)\b"),
        ("fees", r"\bfees?\b"),
        ("certified", r"\bcertif(?:ied|icate|ication)\b"),
        ("licensed", r"\blicen[cs]ed\b"),
        ("official", r"\bofficial(?:ly)?\b"),
        ("money-back", r"\bmoney[\s-]*back\b"),
        ("refund", r"\brefund(?:s|ed|able)?\b"),
        ("insured", r"\binsured\b"),
        ("Siiqo endorsement", r"\bsiiqo\b"),
    )
)

# Allowed only when the same pattern matches the seller's own answers.
CLAIM_WORDS: tuple[tuple[str, re.Pattern], ...] = tuple(
    (label, re.compile(pattern, re.IGNORECASE)) for label, pattern in (
        ("authentic", r"\bauthentic(?:ity)?\b"),
        ("original", r"\boriginals?\b"),
        ("organic", r"\borganic\b"),
        ("natural", r"\b(?:all[\s-]+)?natural\b"),
        ("100%", r"\b100\s*%|\b100\s+percent\b"),
        ("best", r"\bbest\b"),
        ("number one", r"\bnumber\s+one\b|\bno\.?\s*1\b|#\s*1\b"),
        ("leading", r"\bleading\b"),
        ("world-class", r"\bworld[\s-]+class\b"),
        ("trusted", r"\btrust(?:ed|worthy)?\b"),
        ("award", r"\baward(?:s|ed)?\b"),
        ("tested", r"\b(?:dermatologist|clinically|lab)[\s-]+tested\b"),
        ("fast delivery", r"\b(?:fast|quick|express|speedy|rapid)\s+(?:delivery|dispatch|shipping)\b"),
        ("free delivery", r"\bfree\s+(?:delivery|shipping)\b"),
        ("same-day", r"\bsame[\s-]+day\b"),
        ("next-day", r"\bnext[\s-]+day\b"),
        ("overnight", r"\bovernight\b"),
        ("delivery time", r"\bwithin\s+(?:\d+|an?|one|two|three|24|48)\s*(?:hours?|hrs?|days?|minutes?|mins?|weeks?)\b"),
        ("24/7", r"\b24\s*/\s*7\b|\baround\s+the\s+clock\b"),
    )
)

# Places the copy may mention only when the answers do.
PLACES: tuple[str, ...] = (
    # Countries / regions
    "nigeria", "nigerian", "ghana", "kenya", "south africa", "africa", "african", "uk",
    "united kingdom", "london", "usa", "united states", "america", "american", "china",
    "dubai", "uae", "turkey", "italy", "italian", "france", "paris", "europe", "canada",
    # States and FCT
    "abia", "adamawa", "akwa ibom", "anambra", "bauchi", "bayelsa", "benue", "borno",
    "cross river", "delta", "ebonyi", "edo", "ekiti", "enugu", "gombe", "imo", "jigawa",
    "kaduna", "kano", "katsina", "kebbi", "kogi", "kwara", "lagos", "nasarawa", "niger",
    "ogun", "ondo", "osun", "oyo", "plateau", "rivers", "sokoto", "taraba", "yobe",
    "zamfara", "fct", "abuja",
    # Cities and well-known areas
    "ikeja", "lekki", "ajah", "yaba", "surulere", "ikoyi", "victoria island", "festac",
    "ibadan", "port harcourt", "benin", "onitsha", "aba", "owerri", "warri", "jos",
    "ilorin", "abeokuta", "calabar", "uyo", "asaba", "akure", "osogbo", "yola",
    "maiduguri", "zaria", "nsukka", "makurdi", "lokoja", "minna", "awka", "umuahia",
    "wuse", "garki", "maitama", "gwarinpa", "kubwa",
)
_PLACE_RES = tuple((p, re.compile(r"\b" + re.escape(p) + r"\b", re.IGNORECASE)) for p in PLACES)

# Numbers with an optional thousand/million suffix: 15k, 1.2m, 25,000, 2019.
_NUMBER_RE = re.compile(r"(?<![\w.])(\d[\d,]*(?:\.\d+)?)\s*([km])?(?![a-z])", re.IGNORECASE)

LISTING_LIMITS = {"title": 80, "description": 600, "category": 60}


# ── Helpers ─────────────────────────────────────────────────────────────────
def flatten_text(value: Any) -> str:
    """All strings and numbers in a nested value, joined (the 'answers text')."""
    parts: list[str] = []

    def walk(v):
        if isinstance(v, str):
            parts.append(v)
        elif isinstance(v, bool) or v is None:
            return
        elif isinstance(v, (int, float)):
            parts.append(str(v))
        elif isinstance(v, dict):
            for item in v.values():
                walk(item)
        elif isinstance(v, (list, tuple)):
            for item in v:
                walk(item)

    walk(value)
    return "\n".join(parts)


def numbers_in(text: str) -> set[float]:
    """Every number in the text, with k/m suffixes expanded (15k -> 15000 and 15)."""
    found: set[float] = set()
    for raw, suffix in _NUMBER_RE.findall(text or ""):
        try:
            n = float(raw.replace(",", ""))
        except ValueError:
            continue
        found.add(n)
        if suffix:
            found.add(n * (1000 if suffix.lower() == "k" else 1_000_000))
    return found


def banned_claims(text: str, answers_text: str = "") -> list[str]:
    """Labels of banned claims found in ``text`` (claim words pass if the answers use them)."""
    found = [label for label, rx in HARD_BANNED if rx.search(text or "")]
    found += [label for label, rx in CLAIM_WORDS
              if rx.search(text or "") and not rx.search(answers_text or "")]
    return found


def unsupported_facts(text: str, answers_text: str) -> list[str]:
    """Numbers and places in ``text`` that the answers never mention."""
    problems: list[str] = []
    allowed = numbers_in(answers_text)
    # numbers_in expands k/m, so answers "15k" allow copy "15,000".
    for n in sorted(numbers_in(text) - allowed):
        problems.append(f"number {n:g} is not in the seller's answers")
    for place, rx in _PLACE_RES:
        if rx.search(text or "") and not rx.search(answers_text or ""):
            problems.append(f"place '{place}' is not in the seller's answers")
    return problems


def _check_text(where: str, text: str, answers_text: str) -> list[str]:
    problems = [f"{where}: banned claim '{label}'" for label in banned_claims(text, answers_text)]
    problems += [f"{where}: {p}" for p in unsupported_facts(text, answers_text)]
    return problems


# ── Store Spec ──────────────────────────────────────────────────────────────
def check_spec(spec: Any, answers: dict, *, extra_facts: Iterable[str] = ()) -> list[str]:
    parsed, errors = parse_store_spec(spec)
    if errors:
        return [f"schema: {e}" for e in errors]
    problems: list[str] = []
    sections = parsed.get("sections")
    if not sections:
        problems.append("sections: missing")
    else:
        missing = [s for s in REQUIRED_SECTIONS if s not in sections]
        if missing:
            problems.append(f"sections: missing required {', '.join(missing)}")
        if len(set(sections)) != len(sections):
            problems.append("sections: duplicates")
    if not (parsed.get("hero") or {}).get("heading"):
        problems.append("hero.heading: missing")
    answers_text = "\n".join([flatten_text(answers), *extra_facts])
    for where, text in text_fields(parsed).items():
        problems += _check_text(where, text, answers_text)
    return problems


# ── Listing drafts ──────────────────────────────────────────────────────────
def check_listing(listing: Any, answers: dict, *, seller_note: str = "",
                  categories: Iterable[str] | None = None) -> list[str]:
    if not isinstance(listing, dict):
        return ["listing: expected an object"]
    problems: list[str] = []
    answers_text = "\n".join([flatten_text(answers), seller_note or ""])
    title = listing.get("title")
    if not isinstance(title, str) or not title.strip():
        problems.append("title: missing")
    for key, limit in LISTING_LIMITS.items():
        v = listing.get(key)
        if isinstance(v, str) and len(v.strip()) > limit:
            problems.append(f"{key}: at most {limit} characters")
    category = listing.get("category")
    if categories is not None and category not in (None, "") and category not in set(categories):
        problems.append(f"category: '{category}' is not one of ours")
    attrs = listing.get("attributes") or []
    if not isinstance(attrs, list) or len(attrs) > 8:
        problems.append("attributes: at most 8")
        attrs = []
    texts = {"title": title or "", "description": listing.get("description") or ""}
    for i, a in enumerate(attrs):
        if isinstance(a, dict):
            texts[f"attributes[{i}]"] = f"{a.get('name', '')}: {a.get('value', '')}"
    for where, text in texts.items():
        if isinstance(text, str):
            problems += _check_text(where, text, answers_text)
    return problems
