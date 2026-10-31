"""
Onboarding build workflow (Siiqo 2.0, Phase 1): interview answers + photos
-> draft Store Spec + draft listings. Runs as the ``onboarding_build`` job.

Stages (recorded on the session as each one finishes, for polling):
  colours   code: the preset's palette, or the logo's main colour;
  copy      AI (store_spec prompt) -> Store Spec parts, checked by the code
            critic, retried at most twice with the critic's notes, then the
            fallback: the nearest preset with the seller's own words;
  products  AI vision (listing_from_photo prompt), one call per photo (max 5),
            critic + 2 retries each, fallback: an empty draft with the photo;
  assemble  code: the full Store Spec, validated, saved as a draft.

copy and products run in parallel. Nothing public changes: the drafts live on
the session until the seller publishes. With AI off (``AI_ENABLED`` unset),
capped, failing or rejected, the session ends ``ready_fallback``.
"""
from __future__ import annotations

import base64
import io
import logging
import os
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any

from flask import current_app

from app.extensions import db
from app.models.jobs import Job
from app.models.onboarding import OnboardingSession, STAGE_KEYS, empty_stages
from app.services import jobs
from app.services.ai import AICapExceeded, AIDisabled, AIError, AIInvalidOutput, run as ai_run
from app.services.onboarding_critic import HARD_BANNED, check_listing, check_spec
from app.services.store_spec import AI_SECTION_IDS, PRESET_IDS, parse_store_spec, text_fields

log = logging.getLogger(__name__)

JOB_TYPE_BUILD = "onboarding_build"
JOB_TYPE_REBUILD = "onboarding_rebuild"
MAX_RETRIES = 2                 # critic retries per part (so up to 3 attempts)
MAX_PRODUCT_PHOTOS = 5
REBUILD_PARTS = ("copy", "products")
REQUIRED_FOR_BUILD = ("business_name", "what_you_sell")

_SERIF = "Playfair Display, Georgia, serif"
_SANS = "Outfit, system-ui, sans-serif"
_BODY = "system-ui, sans-serif"

# Each preset's default look, from the website's StorefrontThemes.ts (the
# theme's defaultPalette colours, fonts and defaultHeroLayout), plus a short
# description the AI uses to pick a preset.
PRESET_THEMES: dict[str, dict[str, str]] = {
    "tech": {"palette": "Midnight Gold", "primary": "#0b1b3b", "secondary": "#f8fafc",
             "heading": _SANS, "hero": "left", "suits": "phones, gadgets, electronics, solar, appliances"},
    "fashion": {"palette": "Monochrome Mono", "primary": "#000000", "secondary": "#f9fafb",
                "heading": _SERIF, "hero": "right", "suits": "clothes, shoes, bags, tailoring, fabrics"},
    "beauty": {"palette": "Rose Quartz", "primary": "#f43f5e", "secondary": "#fff1f2",
               "heading": _SERIF, "hero": "centered", "suits": "skincare, hair, wigs, makeup, perfume, salons"},
    "luxe-serif": {"palette": "Ivory Atelier", "primary": "#f6f1e8", "secondary": "#14110f",
                   "heading": _SERIF, "hero": "centered", "suits": "jewellery, watches, high-end fashion"},
    "food": {"palette": "Burnt Sienna", "primary": "#92400e", "secondary": "#fffbf0",
             "heading": _SANS, "hero": "centered", "suits": "snacks, cakes, small chops, groceries, drinks"},
    "restaurant-dark": {"palette": "Burnt Sienna", "primary": "#92400e", "secondary": "#fffbf0",
                        "heading": _SERIF, "hero": "fullscreen", "suits": "restaurants, lounges, grills, chefs"},
    "real-estate": {"palette": "Steel Ocean", "primary": "#1e3a8a", "secondary": "#f8fafc",
                    "heading": _SANS, "hero": "right", "suits": "property, rentals, shortlets, land"},
    "minimal": {"palette": "Monochrome Mono", "primary": "#000000", "secondary": "#f9fafb",
                "heading": _SANS, "hero": "fullscreen", "suits": "freelancers, designers, consultants, portfolios"},
    "photography": {"palette": "Monochrome Mono", "primary": "#000000", "secondary": "#f9fafb",
                    "heading": _SERIF, "hero": "fullscreen", "suits": "photographers, videographers, studios"},
    "bold-fitness": {"palette": "Crimson Bold", "primary": "#dc2626", "secondary": "#111827",
                     "heading": _SANS, "hero": "centered", "suits": "gyms, trainers, sportswear, supplements"},
    "education-hub": {"palette": "Arctic Blue", "primary": "#0ea5e9", "secondary": "#f0f9ff",
                      "heading": _SANS, "hero": "left", "suits": "tutors, courses, classes, training"},
    "corporate": {"palette": "Midnight Gold", "primary": "#0b1b3b", "secondary": "#f8fafc",
                  "heading": _SANS, "hero": "left", "suits": "business services, logistics, agencies, cleaning"},
    "playful-dynamic": {"palette": "Rose Quartz", "primary": "#f43f5e", "secondary": "#fff1f2",
                        "heading": _SANS, "hero": "centered", "suits": "toys, kids, gifts, party supplies"},
    "market": {"palette": "Olive Garden", "primary": "#3d6b35", "secondary": "#f7faf6",
               "heading": _SANS, "hero": "centered", "suits": "general goods, mixed products, market stalls"},
}
FALLBACK_PRESET = "market"

# Keyword -> preset, for the fallback's "nearest preset". Earlier presets win ties.
PRESET_KEYWORDS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("restaurant-dark", ("restaurant", "lounge", "grill", "suya", "chef", "bistro", "eatery")),
    ("food", ("food", "snack", "cake", "bakery", "bread", "small chops", "grocer", "drink", "juice",
              "cater", "meal", "pastry", "spice", "rice", "zobo")),
    ("luxe-serif", ("jewel", "jewellery", "jewelry", "watch", "gold", "diamond", "luxury")),
    ("beauty", ("beauty", "cosmetic", "skincare", "skin care", "hair", "wig", "makeup", "make-up",
                "perfume", "fragrance", "salon", "spa", "nail", "lash")),
    ("fashion", ("fashion", "cloth", "apparel", "wear", "shoe", "sneaker", "bag", "dress", "boutique",
                 "ankara", "tailor", "fabric", "aso", "kaftan", "agbada", "thrift")),
    ("tech", ("tech", "gadget", "electronic", "phone", "computer", "laptop", "device", "solar",
              "appliance", "charger", "accessor")),
    ("real-estate", ("real estate", "property", "properties", "apartment", "shortlet", "land", "rent",
                     "house", "realtor")),
    ("photography", ("photo", "video", "studio", "camera", "wedding shoot")),
    ("minimal", ("freelance", "designer", "graphic", "portfolio", "developer", "writer", "consult")),
    ("bold-fitness", ("fitness", "gym", "workout", "sport", "supplement", "trainer")),
    ("education-hub", ("education", "tutor", "course", "class", "lesson", "school", "training", "teach")),
    ("playful-dynamic", ("toy", "kids", "children", "baby", "gift", "party", "balloon")),
    ("corporate", ("logistics", "agency", "cleaning", "printing", "b2b", "company", "services")),
)

# Business data that may go to the AI. Never contact_phone, bank details or IDs.
AI_ANSWER_KEYS = ("business_name", "what_you_sell", "category", "who_buys", "city", "state",
                  "location", "delivery", "price_range", "about", "product_type")


# ── Test seam ───────────────────────────────────────────────────────────────
def get_ai_client():
    """The Anthropic client handed to the gateway. None = the gateway's default.
    Tests monkeypatch this to return a fake client."""
    return None


# ── Answers helpers ─────────────────────────────────────────────────────────
def business_inputs(answers: dict) -> dict:
    return {k: answers[k] for k in AI_ANSWER_KEYS if answers.get(k) not in (None, "", [], {})}


def missing_for_build(answers: dict) -> list[str]:
    return [k for k in REQUIRED_FOR_BUILD if not (answers or {}).get(k)]


def nearest_preset(answers: dict) -> str:
    text = " ".join(str((answers or {}).get(k) or "") for k in ("category", "what_you_sell", "about")).lower()
    best, best_score = FALLBACK_PRESET, 0
    for preset, words in PRESET_KEYWORDS:
        score = sum(1 for w in words if w in text)
        if score > best_score:
            best, best_score = preset, score
    return best


def preset_theme(preset: str, logo_colour: str | None = None) -> dict:
    t = PRESET_THEMES.get(preset) or PRESET_THEMES[FALLBACK_PRESET]
    theme = {
        "paletteId": t["palette"], "primaryColor": t["primary"], "secondaryColor": t["secondary"],
        "fontHeading": t["heading"], "fontBody": _BODY,
    }
    if logo_colour:
        theme["primaryColor"] = logo_colour
        theme["paletteId"] = "custom"
    return theme


# ── Colours ─────────────────────────────────────────────────────────────────
def _local_path(url: str) -> str | None:
    if not isinstance(url, str) or not url.startswith("/static/uploads/"):
        return None
    root = os.path.realpath(os.path.join(current_app.root_path, "static", "uploads"))
    path = os.path.realpath(os.path.join(current_app.root_path, url.lstrip("/")))
    if not path.startswith(root + os.sep) or not os.path.isfile(path):
        return None
    return path


def logo_colour(url: str | None) -> str | None:
    """Main colour of a locally stored logo (ignoring near-white, near-black and
    transparent pixels), or None. Remote (S3) logos keep the preset palette."""
    path = _local_path(url) if url else None
    if not path:
        return None
    try:
        from PIL import Image
        with Image.open(path) as img:
            img = img.convert("RGBA")
            img.thumbnail((64, 64))
            counts: dict[tuple, int] = {}
            for r, g, b, a in img.getdata():
                if a < 128 or max(r, g, b) > 235 or max(r, g, b) < 25:
                    continue
                key = (r // 16 * 16 + 8, g // 16 * 16 + 8, b // 16 * 16 + 8)
                counts[key] = counts.get(key, 0) + 1
        if not counts:
            return None
        r, g, b = max(counts, key=counts.get)
        return f"#{min(r, 255):02x}{min(g, 255):02x}{min(b, 255):02x}"
    except Exception:  # noqa: BLE001 - colours are cosmetic; fall back to the preset
        log.info("onboarding: could not read logo colours from %s", url)
        return None


# ── Images for vision ───────────────────────────────────────────────────────
_MAX_SIDE = 1568


def image_source(url: str) -> dict | None:
    """A Messages API image source for a product photo: local uploads are
    resized and sent as base64; https URLs (S3) are passed as url sources."""
    if isinstance(url, str) and url.startswith("https://"):
        return {"type": "url", "url": url}
    path = _local_path(url)
    if not path:
        return None
    source = image_source_from(path)
    if source is None:
        log.info("onboarding: could not prepare image %s", url)
    return source


def image_source_from(fp) -> dict | None:
    """A base64 Messages API image source from a path or file-like object
    (resized to at most _MAX_SIDE px, re-encoded as JPEG), or None when it
    cannot be read as an image."""
    try:
        from PIL import Image
        with Image.open(fp) as img:
            img = img.convert("RGB")
            img.thumbnail((_MAX_SIDE, _MAX_SIDE))
            buf = io.BytesIO()
            img.save(buf, format="JPEG", quality=85)
        return {"type": "base64", "media_type": "image/jpeg",
                "data": base64.b64encode(buf.getvalue()).decode("ascii")}
    except Exception:  # noqa: BLE001
        return None


# ── Fallback copy (no AI) ───────────────────────────────────────────────────
_SENTENCE_RE = re.compile(r"(?<=[.!?])\s+")

# Trust claims a store page must not carry until Siiqo can back them. The
# seller's own practical words (delivery fees, refund policy, "on Siiqo")
# stay; only these sentences are left out of the fallback draft.
_FALLBACK_STRIP = {"verified", "escrow", "buyer protection", "guaranteed", "genuine", "warranty",
                   "certified", "licensed", "official", "money-back", "insured"}
_FALLBACK_STRIP_RES = tuple(rx for label, rx in HARD_BANNED if label in _FALLBACK_STRIP)


def claim_free(text: str | None) -> str:
    """The seller's words minus any sentence containing a trust claim."""
    if not text:
        return ""
    sentences = _SENTENCE_RE.split(str(text).strip())
    kept = [s for s in sentences if not any(rx.search(s) for rx in _FALLBACK_STRIP_RES)]
    return " ".join(kept).strip()


def _cut(text: str, limit: int) -> str:
    text = (text or "").strip()
    if len(text) <= limit:
        return text
    cut = text[: limit - 1].rsplit(" ", 1)[0].rstrip(",;:-")
    return (cut or text[: limit - 1]) + "…"


def _first_sentence(text: str) -> str:
    return _SENTENCE_RE.split(text.strip(), 1)[0] if text else ""


def fallback_copy(answers: dict, preset: str) -> dict:
    """Spec parts from the seller's own words and neutral, claim-free labels."""
    name = (answers.get("business_name") or "").strip() or "Our store"
    sell = claim_free(answers.get("what_you_sell"))
    about = claim_free(answers.get("about")) or sell
    delivery = claim_free(answers.get("delivery"))
    services = answers.get("product_type") == "service"
    place = ", ".join(p for p in (answers.get("city"), answers.get("state")) if p) or (answers.get("location") or "")

    about_text = about
    if place:
        about_text = f"{about_text} Based in {place}.".strip() if about_text else f"Based in {place}."
    hero = {"heading": _cut(name, 60), "cta": "See our services" if services else "See our products"}
    if sell:
        hero["subtext"] = _cut(sell, 200)
    copy_ = {
        "aboutTitle": _cut(f"About {name}", 60),
        "productsHeading": "Our services" if services else "Our products",
        "contactHeading": "Get in touch",
        "contactDescription": _cut(delivery, 240) if delivery else "Send us a message to order or ask a question.",
    }
    if sell:
        copy_["tagline"] = _cut(_first_sentence(sell), 80)
    if about_text:
        copy_["aboutDescription"] = _cut(about_text, 500)
    sections = ["hero", "products"] + (["about"] if about_text else []) + ["contact"]
    return {"preset": preset, "sections": sections, "hero": hero, "copy": copy_}


# ── Spec assembly ───────────────────────────────────────────────────────────
def assemble_spec(parts: dict, theme: dict) -> dict:
    preset = parts["preset"]
    hero = {"layout": PRESET_THEMES.get(preset, PRESET_THEMES[FALLBACK_PRESET])["hero"]}
    hero.update({k: v for k, v in (parts.get("hero") or {}).items() if isinstance(v, str) and v.strip()})
    copy_ = {k: v for k, v in (parts.get("copy") or {}).items() if isinstance(v, str) and v.strip()}
    return {
        "version": 1,
        "preset": preset,
        "theme": dict(theme),
        "sections": list(dict.fromkeys(parts.get("sections") or [])),
        "hero": hero,
        "copy": copy_,
        "nav": {},
        # The website shows these sections, in this order, only for specs
        # saved with sections_control "spec" (storeSpec/sections.ts).
        "extra": {"sections_control": "spec"},
    }


# ── AI parts ────────────────────────────────────────────────────────────────
class _Outcome:
    def __init__(self):
        self.source = "fallback"
        self.reason: str | None = None
        self.problems: list[str] = []
        self.ai_calls = 0
        self.value: Any = None


def _ai_with_critic(action: str, seller_id: int, inputs: dict, critic, *, images=None,
                    retries: int = MAX_RETRIES) -> _Outcome:
    """Call the gateway, run the critic, retry at most ``retries`` times with
    the critic's notes. Returns an outcome whose value is None on fallback."""
    out = _Outcome()
    feedback: list[str] = []
    for attempt in range(1 + retries):
        call_inputs = dict(inputs)
        if feedback:
            call_inputs["critic_feedback"] = feedback
        try:
            result = ai_run(action, seller_id, call_inputs, client=get_ai_client(), images=images)
        except (AIDisabled, AICapExceeded) as exc:
            out.reason = f"{type(exc).__name__}: {exc.reason or exc}"
            return out
        except AIInvalidOutput as exc:
            out.ai_calls += 1
            feedback = [f"output was invalid: {exc}"]
            out.reason = f"AIInvalidOutput: {exc.reason}"
            continue
        except AIError as exc:
            out.ai_calls += 1
            out.reason = f"{type(exc).__name__}: {exc.reason or exc}"
            return out
        out.ai_calls += 1
        value, problems = critic(result.data)
        if not problems:
            out.source, out.value, out.reason, out.problems = "ai", value, None, []
            return out
        out.problems = problems
        out.reason = "critic rejected the draft"
        feedback = problems[:20]
    return out


def _copy_part(app, seller_id: int, answers: dict, theme_hint: dict, suggested: str) -> _Outcome:
    with app.app_context():
        try:
            inputs = {
                "answers": business_inputs(answers),
                "presets": [{"id": p, "suits": PRESET_THEMES[p]["suits"]} for p in PRESET_IDS],
                "suggested_preset": suggested,
                "allowed_sections": list(AI_SECTION_IDS),
            }

            def critic(data):
                if not isinstance(data, dict) or data.get("preset") not in PRESET_IDS:
                    return None, ["preset: not one of the presets"]
                spec = assemble_spec(data, preset_theme(data["preset"], theme_hint.get("logo")))
                return data, check_spec(spec, answers)

            return _ai_with_critic("store_spec", seller_id, inputs, critic)
        finally:
            db.session.remove()


def listing_from_image(seller_id: int, source: dict, answers: dict, *, seller_note: str = "",
                       categories: list[str], retries: int = MAX_RETRIES) -> _Outcome:
    """One listing draft from one product photo: the listing_from_photo prompt
    (vision) checked by the code critic. Shared by the onboarding build and
    the product form's "Fill from photo" (POST /api/vendor/products/draft-from-photo)."""
    inputs = {
        "business_name": answers.get("business_name"),
        "what_you_sell": answers.get("what_you_sell"),
        "seller_note": seller_note or "",
        "categories": categories,
    }

    def critic(data):
        problems = check_listing(data, answers, seller_note=seller_note or "",
                                 categories=categories if categories else None)
        return data, problems

    return _ai_with_critic("listing_from_photo", seller_id, inputs, critic, images=[source],
                           retries=retries)


def listing_fields(ai: dict, categories: list[str]) -> dict:
    """The editable fields of an AI listing draft, trimmed to the form's limits.
    Never a price: the seller sets it."""
    attrs = [{"name": str(a.get("name", ""))[:40], "value": str(a.get("value", ""))[:60]}
             for a in (ai.get("attributes") or [])[:8] if isinstance(a, dict)]
    return {
        "title": (ai.get("title") or "").strip()[:80],
        "description": (ai.get("description") or "").strip()[:600],
        "category": ai.get("category") if ai.get("category") in categories else None,
        "attributes": attrs,
    }


def _listing_part(app, seller_id: int, answers: dict, photo: dict, categories: list[str]) -> _Outcome:
    with app.app_context():
        try:
            source = image_source(photo["url"])
            if source is None:
                out = _Outcome()
                out.reason = "photo could not be read"
                return out
            return listing_from_image(seller_id, source, answers, seller_note=photo.get("caption") or "",
                                      categories=categories)
        finally:
            db.session.remove()


def _listing_draft(photo: dict, ai: dict | None, categories: list[str]) -> dict:
    draft = {
        "id": f"p{photo['id']}",
        "photo_id": photo["id"],
        "image_url": photo["url"],
        "title": photo.get("caption") or "",
        "description": "",
        "price": photo.get("price"),
        "category": None,
        "attributes": [],
        "quantity": None,
        "source": "fallback",
    }
    if ai:
        fields = listing_fields(ai, categories)
        fields["title"] = fields["title"] or draft["title"]
        draft.update(fields, source="ai")
    return draft


# ── Stage bookkeeping ───────────────────────────────────────────────────────
def _set_stage(session: OnboardingSession, key: str, status: str, **extra) -> None:
    stages = [dict(s) for s in (session.stages or empty_stages())]
    for s in stages:
        if s["key"] == key:
            s["status"] = status
            s.update(extra)
    session.stages = stages
    db.session.commit()


def _fail_open_stages(session: OnboardingSession) -> None:
    stages = [dict(s) for s in (session.stages or empty_stages())]
    for s in stages:
        if s.get("status") in ("pending", "running"):
            s["status"] = "failed"
    session.stages = stages


def recover_session(session: OnboardingSession, job: Job | None, *, reason: str = "") -> bool:
    """A build whose job failed or went stale: put the session back where it
    was (``prev_status`` from the job payload) and mark unfinished stages failed."""
    if session.status != "building":
        return False
    if job is not None and job.status not in ("failed", "done"):
        return False
    prev = (job.payload or {}).get("prev_status") if job is not None else None
    if prev not in ("interview", "ready", "ready_fallback"):
        prev = "ready_fallback" if session.draft_spec else "interview"
    session.status = prev
    _fail_open_stages(session)
    meta = dict(session.build_meta or {})
    meta["last_error"] = reason or (job.error if job is not None else "build stopped")
    session.build_meta = meta
    db.session.commit()
    return True


# ── The job ─────────────────────────────────────────────────────────────────
def category_names() -> list[str]:
    try:
        from app.models.product import Category
        return [c.name for c in Category.query.order_by(Category.name).limit(60).all() if c.name]
    except Exception:  # noqa: BLE001
        db.session.rollback()
        return []


def _photo_dicts(session: OnboardingSession) -> list[dict]:
    return [
        {"id": p.id, "url": p.url, "caption": p.caption,
         "price": float(p.price) if p.price is not None else None}
        for p in session.product_photos()[:MAX_PRODUCT_PHOTOS]
    ]


def _run(session_id: int, job: Job, part: str | None) -> dict:
    app = current_app._get_current_object()
    session = db.session.get(OnboardingSession, session_id)
    if session is None:
        raise LookupError(f"onboarding session {session_id} not found")
    answers = dict(session.answers or {})
    seller_id = session.user_id
    do_copy = part in (None, "copy")
    do_products = part in (None, "products")
    meta = dict(session.build_meta or {}) if part else {}
    meta.pop("last_error", None)

    session.job_id = job.id
    stages = [dict(s) for s in (session.stages or empty_stages())]
    for s in stages:
        if part is None or s["key"] in ({"copy", "colours", "assemble"} if do_copy else {"products", "assemble"}):
            s["status"] = "running" if s["key"] != "assemble" else "pending"
            s.pop("source", None)
    session.stages = stages
    db.session.commit()

    categories = category_names()
    photos = _photo_dicts(session)
    logo = session.logo()
    suggested = nearest_preset(answers)

    # colours (code, instant)
    theme_hint: dict[str, Any] = {"logo": meta.get("logo_colour")}
    if do_copy:
        theme_hint["logo"] = logo_colour(logo.url) if logo else None
        meta["logo_colour"] = theme_hint["logo"]
        _set_stage(session, "colours", "done", source="logo" if theme_hint["logo"] else "preset")

    copy_out: _Outcome | None = None
    listing_outs: dict[int, _Outcome] = {}
    with ThreadPoolExecutor(max_workers=3) as pool:
        futures = {}
        if do_copy:
            futures[pool.submit(_copy_part, app, seller_id, answers, theme_hint, suggested)] = ("copy", None)
        if do_products:
            for photo in photos:
                futures[pool.submit(_listing_part, app, seller_id, answers, photo, categories)] = ("products", photo["id"])
            if not photos:
                _set_stage(session, "products", "done", source="none")
        pending_listings = sum(1 for k, _ in futures.values() if k == "products")
        for fut in as_completed(futures):
            kind, photo_id = futures[fut]
            outcome = fut.result()
            if kind == "copy":
                copy_out = outcome
                _set_stage(session, "copy", "done", source=outcome.source)
            else:
                listing_outs[photo_id] = outcome
                pending_listings -= 1
                if pending_listings == 0:
                    sources = {o.source for o in listing_outs.values()}
                    _set_stage(session, "products", "done", source="ai" if sources == {"ai"} else
                               ("fallback" if sources == {"fallback"} else "mixed"))

    # assemble (code)
    session = db.session.get(OnboardingSession, session_id)
    db.session.refresh(session)
    ai_calls = 0
    if do_copy:
        ai_calls += copy_out.ai_calls
        if copy_out.source == "ai":
            parts = copy_out.value
        else:
            parts = fallback_copy(answers, suggested)
        spec = assemble_spec(parts, preset_theme(parts["preset"], theme_hint["logo"]))
        spec, errors = parse_store_spec(spec)
        if errors:  # the fallback is code; this would be a bug, so fail loudly
            raise ValueError(f"assembled spec is invalid: {errors}")
        session.draft_spec = spec
        meta["copy_source"] = copy_out.source
        meta["copy_fallback_reason"] = copy_out.reason
        meta["copy_critic_problems"] = copy_out.problems[:20]
        meta["preset"] = spec["preset"]
    if do_products:
        drafts = []
        for photo in photos:
            o = listing_outs.get(photo["id"])
            ai_calls += o.ai_calls if o else 0
            drafts.append(_listing_draft(photo, o.value if o and o.source == "ai" else None, categories))
        session.draft_listings = drafts
        meta["listing_sources"] = {d["id"]: d["source"] for d in drafts}
        reasons = {f"p{pid}": o.reason for pid, o in listing_outs.items() if o.reason}
        meta["listing_fallback_reasons"] = reasons

    meta["ai_calls"] = int(meta.get("ai_calls") or 0) + ai_calls if part else ai_calls
    fell_back = meta.get("copy_source") != "ai" or any(
        v != "ai" for v in (meta.get("listing_sources") or {}).values())
    session.status = "ready_fallback" if fell_back else "ready"
    session.build_meta = meta
    _set_stage(session, "assemble", "done")
    return {"status": session.status, "ai_calls": ai_calls, "ai_used": ai_calls > 0,
            "copy_source": meta.get("copy_source"), "part": part}


def _handle(payload: dict, job: Job) -> dict:
    session_id = payload["session_id"]
    try:
        return _run(session_id, job, payload.get("part"))
    except Exception as exc:
        db.session.rollback()
        session = db.session.get(OnboardingSession, session_id)
        if session is not None and session.status == "building":
            prev = payload.get("prev_status") or ("ready_fallback" if session.draft_spec else "interview")
            session.status = prev
            _fail_open_stages(session)
            meta = dict(session.build_meta or {})
            meta["last_error"] = f"{type(exc).__name__}: {exc}"[:500]
            session.build_meta = meta
            db.session.commit()
        raise


jobs.handler(JOB_TYPE_BUILD)(_handle)
jobs.handler(JOB_TYPE_REBUILD)(_handle)


# ── Chat edit (store_edit prompt) ───────────────────────────────────────────
EDITABLE_FIELDS = (
    "hero.heading", "hero.subtext", "hero.cta",
    "copy.tagline", "copy.aboutTitle", "copy.aboutDescription", "copy.productsHeading",
    "copy.productsSubheading", "copy.contactHeading", "copy.contactDescription",
    "copy.footerTitle", "copy.footerDescription",
)


class EditRejected(Exception):
    def __init__(self, problems: list[str]):
        super().__init__("; ".join(problems[:5]))
        self.problems = problems


def apply_chat_edit(session: OnboardingSession, instruction: str) -> tuple[dict, str]:
    """Run store_edit on the draft spec. Returns (new_spec, summary). Raises
    AIError (AI off/failing) or EditRejected (critic). Does not save."""
    spec = dict(session.draft_spec or {})
    answers = dict(session.answers or {})
    inputs = {
        "request": instruction,
        "current_text": {k: v for k, v in text_fields(spec).items() if k in EDITABLE_FIELDS},
        "editable_fields": list(EDITABLE_FIELDS),
        "current_preset": spec.get("preset"),
        "presets": [{"id": p, "suits": PRESET_THEMES[p]["suits"]} for p in PRESET_IDS],
        "answers": business_inputs(answers),
    }
    result = ai_run("store_edit", session.user_id, inputs, client=get_ai_client())
    data = result.data or {}
    new = {k: (dict(v) if isinstance(v, dict) else (list(v) if isinstance(v, list) else v)) for k, v in spec.items()}
    for change in data.get("changes") or []:
        field, value = change.get("field"), change.get("value")
        if field not in EDITABLE_FIELDS or not isinstance(value, str):
            continue
        group, name = field.split(".", 1)
        new.setdefault(group, {})[name] = value
    preset = data.get("preset")
    if preset and preset in PRESET_IDS and preset != new.get("preset"):
        new["preset"] = preset
        logo = (session.build_meta or {}).get("logo_colour")
        new["theme"] = preset_theme(preset, logo)
        new.setdefault("hero", {})["layout"] = PRESET_THEMES[preset]["hero"]
    problems = check_spec(new, answers, extra_facts=[instruction, *text_fields(spec).values()])
    if problems:
        raise EditRejected(problems)
    parsed, _ = parse_store_spec(new)
    return parsed, (data.get("summary") or "").strip()


__all__ = [
    "JOB_TYPE_BUILD", "JOB_TYPE_REBUILD", "REBUILD_PARTS", "MAX_PRODUCT_PHOTOS", "STAGE_KEYS",
    "apply_chat_edit", "EditRejected", "fallback_copy", "nearest_preset", "missing_for_build",
    "recover_session", "get_ai_client",
]
