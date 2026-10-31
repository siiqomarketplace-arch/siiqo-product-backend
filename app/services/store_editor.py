"""
Store editor helpers (Siiqo 2.0, Phase 1 items 1-6 and 1-8).

  * revisions: a copy of ``template_options`` kept before every editor save,
    AI-refresh save and revert, so the seller can undo;
  * ``ai_edit``: one plain-language change ("make the headline shorter") to
    the spec the seller is editing, with the ``store_edit`` prompt and the
    code critic. Returns a proposal only;
  * ``ai_refresh``: a fresh spec built from the store's real data (name,
    description, categories, product names) with the ``store_spec`` prompt
    and the critic, or, with AI off or rejected, the nearest preset with the
    seller's own words. Returns a proposal only.

Nothing here changes a live store. The seller saves a proposal through the
existing settings endpoint (``PATCH /api/vendor/update-settings`` with
``revision_source``), which keeps a revision first.
"""
from __future__ import annotations

import copy
import logging
from typing import Any

from app.extensions import db
from app.models.store_revision import StoreRevision
from app.services import onboarding_build as build
from app.services.ai import AIError, run as ai_run
from app.services.onboarding_critic import check_spec
from app.services.store_spec import (
    AI_SECTION_IDS,
    PRESET_IDS,
    parse_store_spec,
    spec_from_template_options,
    text_fields,
)

log = logging.getLogger(__name__)

MAX_REVISIONS = 10          # kept (and listed) per store
MAX_PRODUCT_NAMES = 30
MAX_CATEGORIES = 12
# Where the Trust Passport card goes in a fresh spec: before contact, else last.
PASSPORT_SECTION = "passport"


# ── Revisions ───────────────────────────────────────────────────────────────
def record_revision(storefront, source: str, user_id: int | None) -> StoreRevision:
    """Keep a copy of the store's current template_options. Caller commits."""
    if source not in StoreRevision.SOURCES:
        raise ValueError(f"unknown revision source: {source}")
    rev = StoreRevision(
        storefront_id=storefront.id,
        template_options=copy.deepcopy(storefront.template_options or {}),
        source=source,
        created_by=user_id,
    )
    db.session.add(rev)
    db.session.flush()
    _prune(storefront.id)
    return rev


def _prune(storefront_id: int) -> None:
    keep = [r.id for r in StoreRevision.query.filter_by(storefront_id=storefront_id)
            .order_by(StoreRevision.created_at.desc(), StoreRevision.id.desc())
            .limit(MAX_REVISIONS).all()]
    if keep:
        (StoreRevision.query
         .filter(StoreRevision.storefront_id == storefront_id, ~StoreRevision.id.in_(keep))
         .delete(synchronize_session=False))


def list_revisions(storefront_id: int) -> list[StoreRevision]:
    return (StoreRevision.query.filter_by(storefront_id=storefront_id)
            .order_by(StoreRevision.created_at.desc(), StoreRevision.id.desc())
            .limit(MAX_REVISIONS).all())


def revert(storefront, revision: StoreRevision, user_id: int | None) -> dict:
    """Save the current state as a revision, then restore ``revision``. Caller commits."""
    restored = copy.deepcopy(revision.template_options or {})
    record_revision(storefront, "revert", user_id)
    storefront.template_options = restored
    return restored


# ── Store facts (what the AI and the critic may use) ────────────────────────
def store_facts(storefront) -> dict:
    """The store's own public business data. Never phone, bank or ID data."""
    from app.models.product import Product

    products = (Product.query.filter_by(storefront_id=storefront.id, is_active=True)
                .order_by(Product.id.desc()).limit(MAX_PRODUCT_NAMES).all())
    names = [p.name.strip() for p in products if (p.name or "").strip()]
    categories: list[str] = []
    for p in products:
        name = getattr(getattr(p, "category", None), "name", None)
        if name and name not in categories:
            categories.append(name)
    types = {(p.product_type or "physical") for p in products}
    return {
        "business_name": (storefront.store_name or "").strip(),
        "description": (storefront.store_description or "").strip(),
        "categories": categories[:MAX_CATEGORIES],
        "product_names": names,
        "city": (storefront.city or "").strip(),
        "state": (storefront.state or "").strip(),
        "product_type": "service" if types == {"service"} else "physical",
    }


def facts_as_answers(facts: dict) -> dict:
    """Store facts in the shape of the onboarding interview answers."""
    description = facts.get("description") or ""
    what = description or ", ".join(facts.get("categories") or []) or ", ".join(
        (facts.get("product_names") or [])[:5])
    answers = {
        "business_name": facts.get("business_name") or "",
        "what_you_sell": what,
        "about": description,
        "category": ", ".join(facts.get("categories") or []),
        "city": facts.get("city") or "",
        "state": facts.get("state") or "",
        "product_type": facts.get("product_type") or "physical",
        # Not sent to the AI (business_inputs keeps only its own keys), but
        # the critic's fact trace may use them: product names hold numbers.
        "product_names": facts.get("product_names") or [],
    }
    return {k: v for k, v in answers.items() if v not in ("", [], None)}


def _ai_inputs(answers: dict) -> dict:
    inputs = build.business_inputs(answers)
    if answers.get("product_names"):
        inputs["product_names"] = answers["product_names"][:MAX_PRODUCT_NAMES]
    return inputs


# ── AI edit (store_edit prompt) ─────────────────────────────────────────────
class EditRejected(Exception):
    def __init__(self, problems: list[str]):
        super().__init__("; ".join(problems[:5]))
        self.problems = problems


def _changed_fields(old: dict, new: dict) -> set[str]:
    before, after = text_fields(old), text_fields(new)
    changed = {k for k in set(before) | set(after) if before.get(k) != after.get(k)}
    if old.get("preset") != new.get("preset"):
        changed.add("preset")
    return changed


def _problems_in(problems: list[str], fields: set[str]) -> list[str]:
    """Schema problems always count; text problems only for fields the AI changed
    (the seller's own untouched text is not the AI's to fix)."""
    out = []
    for p in problems:
        where = p.split(":", 1)[0]
        if where == "schema" or where in fields:
            out.append(p)
    return out


def ai_edit(storefront, user_id: int, spec: dict, instruction: str) -> tuple[dict, str]:
    """Apply one plain-language change to ``spec`` (already validated).
    Returns (proposal, summary). Raises AIError (AI off/failing) or
    EditRejected (critic). Saves nothing."""
    facts = store_facts(storefront)
    answers = facts_as_answers(facts)
    current = {k: v for k, v in text_fields(spec).items() if k in build.EDITABLE_FIELDS}
    inputs = {
        "request": instruction,
        "current_text": current,
        "editable_fields": list(build.EDITABLE_FIELDS),
        "current_preset": spec.get("preset"),
        "presets": [{"id": p, "suits": build.PRESET_THEMES[p]["suits"]} for p in PRESET_IDS],
        "answers": _ai_inputs(answers),
    }
    result = ai_run("store_edit", user_id, inputs, client=build.get_ai_client())
    data = result.data or {}
    new = copy.deepcopy(spec)
    for change in data.get("changes") or []:
        if not isinstance(change, dict):
            continue
        field, value = change.get("field"), change.get("value")
        if field not in build.EDITABLE_FIELDS or not isinstance(value, str):
            continue
        group, name = field.split(".", 1)
        new.setdefault(group, {})[name] = value
    preset = data.get("preset")
    if preset and preset in PRESET_IDS and preset != new.get("preset"):
        new["preset"] = preset
        new["theme"] = {**(new.get("theme") or {}), **build.preset_theme(preset)}
        new.setdefault("hero", {})["layout"] = build.PRESET_THEMES[preset]["hero"]
    problems = check_spec(new, answers, extra_facts=[instruction, *text_fields(spec).values()])
    problems = _problems_in(problems, _changed_fields(spec, new))
    if problems:
        raise EditRejected(problems)
    parsed, errors = parse_store_spec(new)
    if errors:  # check_spec parses first, so this would be a bug
        raise EditRejected([f"schema: {e}" for e in errors])
    return parsed, (data.get("summary") or "").strip()


# ── AI refresh (store_spec prompt, or the preset fallback) ──────────────────
def _with_passport(sections: list[str]) -> list[str]:
    out = [s for s in sections if s != PASSPORT_SECTION]
    at = out.index("contact") if "contact" in out else len(out)
    out.insert(at, PASSPORT_SECTION)
    return out


def ai_refresh(storefront, user_id: int) -> dict:
    """A fresh Store Spec proposal for this store. Never raises for AI
    problems: falls back to the nearest preset with the seller's own words.
    Returns {proposal, source, reason, problems}. Saves nothing."""
    facts = store_facts(storefront)
    answers = facts_as_answers(facts)
    answers.setdefault("business_name", storefront.store_name or "Our store")
    suggested = build.nearest_preset(answers)

    def critic(data):
        if not isinstance(data, dict) or data.get("preset") not in PRESET_IDS:
            return None, ["preset: not one of the presets"]
        spec = build.assemble_spec(data, build.preset_theme(data["preset"]))
        return data, check_spec(spec, answers)

    inputs = {
        "answers": _ai_inputs(answers),
        "presets": [{"id": p, "suits": build.PRESET_THEMES[p]["suits"]} for p in PRESET_IDS],
        "suggested_preset": suggested,
        "allowed_sections": list(AI_SECTION_IDS),
    }
    outcome = build._ai_with_critic("store_spec", user_id, inputs, critic)
    parts = outcome.value if outcome.source == "ai" else build.fallback_copy(answers, suggested)
    spec = build.assemble_spec(parts, build.preset_theme(parts["preset"]))

    # Keep the store's options this spec version doesn't model (logo
    # settings, per-template copy, ...); show the Trust Passport card.
    current = spec_from_template_options(storefront.template_options or {})
    spec["extra"] = {**(current.get("extra") or {}), "sections_control": "spec"}
    spec["sections"] = _with_passport(spec.get("sections") or [])
    parsed, errors = parse_store_spec(spec)
    if errors:  # the fallback is code; this would be a bug
        raise ValueError(f"refresh spec is invalid: {errors}")
    return {
        "proposal": parsed,
        "source": outcome.source,
        "reason": outcome.reason,
        "problems": outcome.problems[:10],
    }


__all__ = [
    "MAX_REVISIONS", "record_revision", "list_revisions", "revert", "store_facts",
    "facts_as_answers", "ai_edit", "ai_refresh", "EditRejected", "AIError",
]
