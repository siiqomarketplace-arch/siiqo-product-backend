"""Removed content: Siiqo doesn't give grants, loans or funding.

The Grants Hub, grant/funding blog articles, the Siiqo Business plans and
the financing partner type were removed on purpose. Data stays in the
database; only public access is closed. Removed things answer
HTTP 410 {"code": "REMOVED"}.

Grant-article rule (deterministic, no guessing about production rows). An
article is removed when ANY of these is true:
  1. its category contains "grant" or "funding" (case-insensitive), which
     catches the old "Grants & Funding" category;
  2. its sub_category is one of the old Grants Hub filters (GRANT_SUB_CATEGORIES);
  3. its slug, split on "-", contains one of REMOVED_SLUG_WORDS;
  4. its slug is in KNOWN_REMOVED_SLUGS (grant-programme articles whose slug
     has none of the words).
The website (src/lib/removedContent.ts) applies rules 3 and 4 locally and
asks /api/marketplace/blog-removed for the rest. Keep both lists in step.
"""
from flask import jsonify
from sqlalchemy import and_, func, or_

REMOVED_MESSAGE = "This has been removed. Siiqo doesn't give grants, loans or funding."

REMOVED_SLUG_WORDS = (
    "grant", "grants", "funding", "loan", "loans",
    "lender", "lenders", "microloan", "microloans",
)

KNOWN_REMOVED_SLUGS = (
    "niya-x-cascador-founders-programme-how-to-apply",
    "niya-cascador-founders-programme-how-to-apply",
    "how-to-apply-for-the-offchart-nxt-startup-challenge-2026",
    "startup-south-pledges-30m-for-startup-challenge-at-offchart-nxt",
    "ecowas-65000-startup-awards-2026-how-to-apply",
)

# The Grants Hub filters the admin blog editor used to set (only ever set
# for the "Grants & Funding" category).
GRANT_SUB_CATEGORIES = (
    "open", "open-grants", "open grants", "closing-soon", "closing soon",
    "closing", "upcoming", "upcoming-grants", "upcoming grants", "nigeria",
    "nigeria specific", "africa", "pan-africa", "pan africa", "women",
    "women entrepreneurs", "youth", "youth & tech", "startups", "startup",
    "small-business", "small business", "guides", "grant-guides",
    "grant guides",
)

# Partner types no longer accepted for new applications.
REMOVED_PARTNER_TYPES = ("FINANCING",)


def removed_response():
    return jsonify({"code": "REMOVED", "message": REMOVED_MESSAGE}), 410


def is_removed_slug(slug):
    s = (slug or "").strip().lower()
    if not s:
        return False
    if s in KNOWN_REMOVED_SLUGS:
        return True
    return any(word in REMOVED_SLUG_WORDS for word in s.split("-"))


def is_removed_article(article):
    """Python form of the rule, for one Article row."""
    category = (getattr(article, "category", None) or "").lower()
    if "grant" in category or "funding" in category:
        return True
    sub = (getattr(article, "sub_category", None) or "").strip().lower()
    if sub and sub in GRANT_SUB_CATEGORIES:
        return True
    return is_removed_slug(getattr(article, "slug", None))


def _slug_has_word(column, word):
    return or_(
        column == word,
        column.like(f"{word}-%"),
        column.like(f"%-{word}"),
        column.like(f"%-{word}-%"),
    )


def removed_article_clause(Article):
    """SQL form of the same rule (exactly equivalent to is_removed_article)."""
    category = func.lower(func.coalesce(Article.category, ""))
    sub = func.lower(func.trim(func.coalesce(Article.sub_category, "")))
    slug = func.lower(Article.slug)
    return or_(
        category.like("%grant%"),
        category.like("%funding%"),
        and_(sub != "", sub.in_(GRANT_SUB_CATEGORIES)),
        slug.in_(KNOWN_REMOVED_SLUGS),
        *[_slug_has_word(slug, w) for w in REMOVED_SLUG_WORDS],
    )


# Raw-SQL form for the schema-fallback paths in public.py (no sub_category
# column there, so rules 1, 3 and 4 only).
def removed_article_sql():
    parts = ["LOWER(COALESCE(category, '')) LIKE '%grant%'",
             "LOWER(COALESCE(category, '')) LIKE '%funding%'"]
    known = ", ".join(f"'{s}'" for s in KNOWN_REMOVED_SLUGS)
    parts.append(f"LOWER(slug) IN ({known})")
    for w in REMOVED_SLUG_WORDS:
        parts.append(
            f"(LOWER(slug) = '{w}' OR LOWER(slug) LIKE '{w}-%' "
            f"OR LOWER(slug) LIKE '%-{w}' OR LOWER(slug) LIKE '%-{w}-%')"
        )
    return "(" + " OR ".join(parts) + ")"
