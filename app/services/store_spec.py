"""
Store Spec v1 in Python - mirrors the website's ``src/lib/storeSpec``:

  * ``presets.ts``  -> PRESET_IDS, DEFAULT_PRESET, resolve_preset_id, PRESET_LAYOUT_STYLE
  * ``spec.ts``     -> SECTION_IDS, parse_store_spec (the zod schema),
                       spec_from_template_options, template_options_from_spec

Keep the two in step: a change to either side needs the same change here.
The spec is stored as ``Storefront.template_options`` exactly as today.
"""
from __future__ import annotations

import copy
import re
from typing import Any

# ── presets.ts ──────────────────────────────────────────────────────────────
PRESET_IDS: tuple[str, ...] = (
    "tech",
    "fashion",
    "beauty",
    "luxe-serif",
    "food",
    "restaurant-dark",
    "real-estate",
    "minimal",
    "photography",
    "bold-fitness",
    "education-hub",
    "corporate",
    "playful-dynamic",
    "market",
)

DEFAULT_PRESET = "tech"

# Order matters: the LAST preset listing a value wins ("Luxe Serif" -> luxe-serif).
LAYOUT_ALIASES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("fashion", ("Fashion Chic", "Luxe Serif", "TemplateFashion")),
    ("beauty", ("Beauty & Wellness", "TemplateBeauty")),
    ("luxe-serif", ("Luxe Serif", "TemplateLuxeSerif")),
    ("food", ("Street Food", "TemplateFood")),
    ("restaurant-dark", ("Restaurant Dark", "TemplateRestaurantDark")),
    ("real-estate", ("Real Estate", "TemplateRealEstate")),
    ("minimal", ("Dark Portfolio", "Freelancer Pro", "TemplateMinimal")),
    ("photography", ("Photography",)),
    ("bold-fitness", ("Bold Fitness", "Bold Energetic", "TemplateBoldFitness")),
    ("education-hub", ("Education Hub",)),
    ("corporate", ("Corporate Pro",)),
    ("playful-dynamic", ("Playful Dynamic",)),
    ("market", ("African Market", "TemplateMarket")),
)

KNOWN_LAYOUT_STYLES: tuple[str, ...] = tuple(dict.fromkeys(n for _, names in LAYOUT_ALIASES for n in names))


def resolve_preset_id(layout_style: str | None) -> str:
    preset = DEFAULT_PRESET
    for preset_id, names in LAYOUT_ALIASES:
        if layout_style is not None and layout_style in names:
            preset = preset_id
    return preset


PRESET_LAYOUT_STYLE: dict[str, str] = {
    "tech": "Modern Tech",
    "fashion": "Fashion Chic",
    "beauty": "Beauty & Wellness",
    "luxe-serif": "Luxe Serif",
    "food": "Street Food",
    "restaurant-dark": "Restaurant Dark",
    "real-estate": "Real Estate",
    "minimal": "Dark Portfolio",
    "photography": "Photography",
    "bold-fitness": "Bold Fitness",
    "education-hub": "Education Hub",
    "corporate": "Corporate Pro",
    "playful-dynamic": "Playful Dynamic",
    "market": "African Market",
}

# ── spec.ts ─────────────────────────────────────────────────────────────────
SECTION_IDS: tuple[str, ...] = ("hero", "products", "about", "reviews", "contact", "hours", "atmosphere", "faq")

_HEX = re.compile(r"^#[0-9a-fA-F]{6}$")

# group -> {field: max_length}; colours are checked separately.
GROUP_FIELDS: dict[str, dict[str, int]] = {
    "theme": {
        "paletteId": 60, "primaryColor": 7, "secondaryColor": 7,
        "fontHeading": 60, "fontBody": 60, "storeMode": 40,
    },
    "hero": {"layout": 40, "heading": 120, "subtext": 300, "cta": 40},
    "copy": {
        "tagline": 160, "aboutTitle": 120, "aboutDescription": 2000,
        "productsHeading": 120, "productsSubheading": 300,
        "testimonialsHeading": 120, "testimonialsIntro": 300,
        "contactHeading": 120, "contactDescription": 500,
        "footerTitle": 120, "footerDescription": 500, "faqHeading": 120,
    },
    "nav": {"home": 30, "products": 30, "about": 30, "reviews": 30, "contact": 30, "hours": 30},
}
COLOUR_FIELDS = ("primaryColor", "secondaryColor")

# template_options key <-> (group, field)
FIELD_MAP: tuple[tuple[str, tuple[str, str]], ...] = (
    ("palette_id", ("theme", "paletteId")),
    ("font_heading", ("theme", "fontHeading")),
    ("font_body", ("theme", "fontBody")),
    ("store_mode", ("theme", "storeMode")),
    ("hero_layout", ("hero", "layout")),
    ("hero_heading", ("hero", "heading")),
    ("hero_subtext", ("hero", "subtext")),
    ("hero_cta", ("hero", "cta")),
    ("store_tagline", ("copy", "tagline")),
    ("about_title", ("copy", "aboutTitle")),
    ("about_description", ("copy", "aboutDescription")),
    ("products_heading", ("copy", "productsHeading")),
    ("products_subheading", ("copy", "productsSubheading")),
    ("testimonials_heading", ("copy", "testimonialsHeading")),
    ("testimonials_intro", ("copy", "testimonialsIntro")),
    ("contact_heading", ("copy", "contactHeading")),
    ("contact_description", ("copy", "contactDescription")),
    ("footer_title", ("copy", "footerTitle")),
    ("footer_description", ("copy", "footerDescription")),
    ("faq_heading", ("copy", "faqHeading")),
    ("nav_home_label", ("nav", "home")),
    ("nav_products_label", ("nav", "products")),
    ("nav_about_label", ("nav", "about")),
    ("nav_reviews_label", ("nav", "reviews")),
    ("nav_contact_label", ("nav", "contact")),
    ("nav_hours_label", ("nav", "hours")),
)

HANDLED = frozenset(
    [k for k, _ in FIELD_MAP]
    + ["layout_style", "sections", "primaryColor", "primary_color", "secondaryColor", "secondary_color"]
)


class StoreSpecError(ValueError):
    def __init__(self, errors: list[str]):
        super().__init__("; ".join(errors[:5]))
        self.errors = errors


def parse_store_spec(value: Any) -> tuple[dict | None, list[str]]:
    """
    Validate untrusted input like ``StoreSpecSchema.safeParse``. Returns
    (spec, []) on success - strings trimmed, unknown keys dropped, missing
    groups defaulted to {} - or (None, errors).
    """
    errors: list[str] = []
    if not isinstance(value, dict):
        return None, ["spec: expected an object"]
    out: dict[str, Any] = {}

    if value.get("version") != 1 or isinstance(value.get("version"), bool):
        errors.append("version: must be 1")
    out["version"] = 1

    preset = value.get("preset")
    if preset not in PRESET_IDS:
        errors.append(f"preset: must be one of {', '.join(PRESET_IDS)}")
    out["preset"] = preset

    for group, fields in GROUP_FIELDS.items():
        raw = value.get(group, {})
        if raw is None or not isinstance(raw, dict):
            errors.append(f"{group}: expected an object")
            continue
        clean: dict[str, str] = {}
        for name, max_len in fields.items():
            if name not in raw:
                continue
            v = raw[name]
            if not isinstance(v, str):
                errors.append(f"{group}.{name}: expected a string")
                continue
            if group == "theme" and name in COLOUR_FIELDS:
                if not _HEX.match(v):
                    errors.append(f"{group}.{name}: use a hex colour like #E0921C")
                    continue
                clean[name] = v
                continue
            v = v.strip()
            if len(v) > max_len:
                errors.append(f"{group}.{name}: at most {max_len} characters")
                continue
            clean[name] = v
        out[group] = clean

    if "sections" in value:
        sections = value["sections"]
        if not isinstance(sections, list):
            errors.append("sections: expected a list")
        elif len(sections) > len(SECTION_IDS):
            errors.append(f"sections: at most {len(SECTION_IDS)} items")
        elif any(s not in SECTION_IDS for s in sections):
            errors.append(f"sections: each must be one of {', '.join(SECTION_IDS)}")
        else:
            out["sections"] = list(sections)

    extra = value.get("extra", {})
    if not isinstance(extra, dict) or any(not isinstance(k, str) for k in extra):
        errors.append("extra: expected an object")
    else:
        out["extra"] = copy.deepcopy(extra)

    if errors:
        return None, errors
    return out, []


def validate_store_spec(value: Any) -> dict:
    spec, errors = parse_store_spec(value)
    if errors:
        raise StoreSpecError(errors)
    return spec


def _str(v: Any) -> str | None:
    return v if isinstance(v, str) and v != "" else None


def spec_from_template_options(tpl: dict | None) -> dict:
    """Read saved template_options into a spec. Never fails: unknown keys go to ``extra``."""
    t = tpl or {}
    spec: dict[str, Any] = {
        "version": 1,
        "preset": resolve_preset_id(_str(t.get("layout_style"))),
        "theme": {}, "hero": {}, "copy": {}, "nav": {}, "extra": {},
    }
    primary = _str(t.get("primaryColor")) or _str(t.get("primary_color"))
    secondary = _str(t.get("secondaryColor")) or _str(t.get("secondary_color"))
    if primary:
        spec["theme"]["primaryColor"] = primary
    if secondary:
        spec["theme"]["secondaryColor"] = secondary
    if isinstance(t.get("sections"), list):
        spec["sections"] = [s for s in t["sections"] if s in SECTION_IDS]
    for key, (group, field) in FIELD_MAP:
        v = _str(t.get(key))
        if v is not None:
            spec[group][field] = v
    for key, v in t.items():
        if key not in HANDLED:
            spec["extra"][key] = v
    return spec


def template_options_from_spec(spec: dict) -> dict:
    """Write a spec back as template_options (what the backend stores and the templates read)."""
    out: dict[str, Any] = {**(spec.get("extra") or {}), "layout_style": PRESET_LAYOUT_STYLE[spec["preset"]]}
    theme = spec.get("theme") or {}
    if theme.get("primaryColor"):
        out["primaryColor"] = theme["primaryColor"]
        out["primary_color"] = theme["primaryColor"]
    if theme.get("secondaryColor"):
        out["secondaryColor"] = theme["secondaryColor"]
        out["secondary_color"] = theme["secondaryColor"]
    if spec.get("sections") is not None:
        out["sections"] = list(spec["sections"])
    for key, (group, field) in FIELD_MAP:
        v = (spec.get(group) or {}).get(field)
        if v is not None:
            out[key] = v
    return out


def text_fields(spec: dict) -> dict[str, str]:
    """Every visible text field of a spec as {"group.field": text} (for the critic)."""
    out: dict[str, str] = {}
    for group in ("hero", "copy", "nav"):
        for field, v in (spec.get(group) or {}).items():
            if isinstance(v, str) and field != "layout":
                out[f"{group}.{field}"] = v
    return out
