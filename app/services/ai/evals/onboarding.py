"""
Eval support for the Phase 1 onboarding prompts: ``store_spec``,
``store_edit`` and ``listing_from_photo``.

Cases for these actions do not hold the raw gateway inputs. They hold the
seller's data, and :func:`prepare` builds the gateway inputs the same way
production does (``onboarding_build``), so a case cannot drift from what the
model really sees:

store_spec::

    "inputs": {"answers": {...interview answers, contact_phone included...}}

store_edit::

    "inputs": {"answers": {...}, "spec": {...current draft Store Spec...},
               "request": "change the headline to ..."}

listing_from_photo::

    "inputs": {"answers": {"business_name": ..., "what_you_sell": ...},
               "seller_note": "...", "categories": ["Fashion", ...],
               "images": [{"path": "listing_from_photo/ankara-dress.jpg",
                           "shows": "what the photo shows, for reviewers"}]}

Image ``path`` is relative to ``evals/images/``. Photos are not kept in the
repo yet: a case whose photo file is missing is reported as skipped in a
live run, never as passed. Offline tests use the ``fixture`` only.

Every onboarding case also has:

``fixture``  a reference output in the prompt's output shape. Offline tests
             run it through the same checks as a live answer, so the case's
             expectations are proven satisfiable and the critic is exercised.

``expect`` keys, on top of the generic ones in ``evals/__init__.py``:

``critic``            true: the code critic (``onboarding_critic``) must pass:
                      schema, required sections, length limits, banned
                      claims, and every number and place traced to the answers.
``allowed_presets``   store_spec: preset must be one of these.
                      store_edit: preset must be null or one of these.
``required_sections`` store_spec: these section ids must be present.
``forbidden_sections`` store_spec: these section ids must be absent.
``only_fields``       store_edit: every change is to one of these fields.
``field_values``      store_edit: these fields are changed to exactly these values.
``no_changes``        store_edit: the changes list must be empty.
``allowed_categories`` listing_from_photo: category must be one of these (null allowed via null).
"""
from __future__ import annotations

import base64
import copy
import mimetypes
from pathlib import Path
from typing import Any

IMAGES_DIR = Path(__file__).resolve().parent / "images"
ACTIONS = ("store_spec", "store_edit", "listing_from_photo")
# The prompts' own hard length limits (stricter than the Store Spec schema the
# critic enforces). A live answer over these is a prompt failure even if the
# critic would accept it.
SPEC_PROMPT_LIMITS = {
    "hero.heading": 60, "hero.subtext": 200, "hero.cta": 24,
    "copy.tagline": 80, "copy.aboutTitle": 60, "copy.aboutDescription": 500,
    "copy.productsHeading": 60, "copy.productsSubheading": 160,
    "copy.contactHeading": 60, "copy.contactDescription": 240,
}
EDIT_PROMPT_LIMITS = {
    "hero.heading": 60, "hero.subtext": 200, "hero.cta": 24,
    "copy.tagline": 80, "copy.aboutTitle": 60, "copy.aboutDescription": 500,
    "copy.productsHeading": 60, "copy.productsSubheading": 200,
    "copy.contactHeading": 60, "copy.contactDescription": 240,
    "copy.footerTitle": 60, "copy.footerDescription": 240,
}
LISTING_PROMPT_LIMITS = {"title": 70, "description": 400}
_IMAGE_TYPES = {".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png", ".webp": "image/webp"}


class MissingImage(Exception):
    """A case's photo is not on disk (photos are added by hand before a live run)."""


def base_action(action: str) -> str:
    return action.split("@", 1)[0]


def is_onboarding(action: str) -> bool:
    return base_action(action) in ACTIONS


# ── Structure ───────────────────────────────────────────────────────────────
def validate_case(action: str, case: dict) -> list[str]:
    """Structure problems in one onboarding case (empty = fine)."""
    name = base_action(action)
    cid = case.get("id", "?")
    problems: list[str] = []
    data = case.get("inputs")
    if not isinstance(data, dict) or not isinstance(data.get("answers"), dict):
        return [f"{cid}: inputs.answers must be an object"]
    if "fixture" not in case:
        problems.append(f"{cid}: needs a 'fixture' reference output")
    expect = case.get("expect") or {}
    if expect.get("critic") is not True:
        problems.append(f"{cid}: expect.critic must be true")
    if not expect.get("must_not_contain"):
        problems.append(f"{cid}: expect.must_not_contain must list banned claims")
    if name == "store_spec":
        if not data["answers"].get("business_name") or not data["answers"].get("what_you_sell"):
            problems.append(f"{cid}: answers need business_name and what_you_sell")
        if not expect.get("allowed_presets"):
            problems.append(f"{cid}: expect.allowed_presets is required")
    elif name == "store_edit":
        if not isinstance(data.get("spec"), dict) or not isinstance(data.get("request"), str):
            problems.append(f"{cid}: store_edit inputs need 'spec' and 'request'")
        if "allowed_presets" not in expect:
            problems.append(f"{cid}: expect.allowed_presets is required (use [] for 'no design change')")
    elif name == "listing_from_photo":
        images = data.get("images")
        if not isinstance(images, list) or not images:
            problems.append(f"{cid}: listing_from_photo needs an images list")
        else:
            for ref in images:
                path = (ref or {}).get("path") if isinstance(ref, dict) else None
                if not path or Path(path).suffix.lower() not in _IMAGE_TYPES or ".." in Path(path).parts:
                    problems.append(f"{cid}: image refs need a relative .jpg/.png/.webp 'path'")
                if not isinstance(ref, dict) or not ref.get("shows"):
                    problems.append(f"{cid}: image refs need 'shows' (what the photo shows)")
        if not isinstance(data.get("categories"), list):
            problems.append(f"{cid}: listing_from_photo needs a categories list")
    return problems


# ── Inputs, built as production builds them ─────────────────────────────────
def _presets() -> list[dict]:
    from app.services.onboarding_build import PRESET_THEMES
    from app.services.store_spec import PRESET_IDS
    return [{"id": p, "suits": PRESET_THEMES[p]["suits"]} for p in PRESET_IDS]


def load_image(ref: dict) -> dict:
    path = IMAGES_DIR / ref["path"]
    if not path.is_file():
        raise MissingImage(f"photo not found: evals/images/{ref['path']}")
    media = _IMAGE_TYPES.get(path.suffix.lower()) or mimetypes.guess_type(path.name)[0] or "image/jpeg"
    return {"type": "base64", "media_type": media, "data": base64.b64encode(path.read_bytes()).decode()}


def prepare(action: str, case: dict) -> tuple[dict, list[dict] | None]:
    """(gateway inputs, image sources) for a case. Raises MissingImage."""
    if not is_onboarding(action):
        return case["inputs"], None
    from app.services.onboarding_build import EDITABLE_FIELDS, business_inputs, nearest_preset
    from app.services.store_spec import AI_SECTION_IDS, text_fields

    name = base_action(action)
    data = case["inputs"]
    answers = data.get("answers") or {}
    if name == "store_spec":
        return {
            "answers": business_inputs(answers),
            "presets": _presets(),
            "suggested_preset": nearest_preset(answers),
            "allowed_sections": list(AI_SECTION_IDS),
        }, None
    if name == "store_edit":
        spec = data["spec"]
        return {
            "request": data["request"],
            "current_text": {k: v for k, v in text_fields(spec).items() if k in EDITABLE_FIELDS},
            "editable_fields": list(EDITABLE_FIELDS),
            "current_preset": spec.get("preset"),
            "presets": _presets(),
            "answers": business_inputs(answers),
        }, None
    if name == "listing_from_photo":
        images = [load_image(ref) for ref in data["images"]]
        return {
            "business_name": answers.get("business_name"),
            "what_you_sell": answers.get("what_you_sell"),
            "seller_note": data.get("seller_note") or "",
            "categories": list(data.get("categories") or []),
        }, images
    raise ValueError(f"no input builder for {action!r}")


# ── Checks ──────────────────────────────────────────────────────────────────
def apply_edit(spec: dict, output: Any) -> dict:
    """The draft after a store_edit answer, applied as ``apply_chat_edit`` does."""
    from app.services.onboarding_build import EDITABLE_FIELDS, PRESET_THEMES, preset_theme
    from app.services.store_spec import PRESET_IDS

    new = copy.deepcopy(spec)
    data = output if isinstance(output, dict) else {}
    for change in data.get("changes") or []:
        if not isinstance(change, dict):
            continue
        field, value = change.get("field"), change.get("value")
        if field not in EDITABLE_FIELDS or not isinstance(value, str):
            continue
        group, key = field.split(".", 1)
        new.setdefault(group, {})[key] = value
    preset = data.get("preset")
    if preset and preset in PRESET_IDS and preset != new.get("preset"):
        new["preset"] = preset
        new["theme"] = preset_theme(preset)
        new.setdefault("hero", {})["layout"] = PRESET_THEMES[preset]["hero"]
    return new


def _over_limits(fields: dict[str, str], limits: dict[str, int]) -> list[str]:
    return [f"{f}: {len(v)} characters, prompt limit {limits[f]}"
            for f, v in fields.items() if f in limits and len(v) > limits[f]]


def check_onboarding(action: str, case: dict, output: Any) -> list[str]:
    """Failures of the onboarding-specific expectations (empty = pass)."""
    from app.services.onboarding_build import EDITABLE_FIELDS, assemble_spec, preset_theme
    from app.services.onboarding_critic import check_listing, check_spec
    from app.services.store_spec import PRESET_IDS, text_fields

    name = base_action(action)
    data = case["inputs"]
    expect = case.get("expect") or {}
    answers = data.get("answers") or {}
    obj = output if isinstance(output, dict) else {}
    failures: list[str] = []

    if name == "store_spec":
        preset = obj.get("preset")
        allowed = expect.get("allowed_presets")
        if allowed and preset not in allowed:
            failures.append(f"preset: {preset!r} is not one of {allowed!r}")
        sections = obj.get("sections") or []
        for s in expect.get("required_sections") or []:
            if s not in sections:
                failures.append(f"sections: {s!r} missing")
        for s in expect.get("forbidden_sections") or []:
            if s in sections:
                failures.append(f"sections: {s!r} not supported by the answers")
        if expect.get("critic"):
            failures += _over_limits(text_fields(obj), SPEC_PROMPT_LIMITS)
            if preset not in PRESET_IDS:
                failures.append("critic: preset: not one of the presets")
            else:
                spec = assemble_spec(obj, preset_theme(preset))
                failures += [f"critic: {p}" for p in check_spec(spec, answers)]

    elif name == "store_edit":
        changes = [c for c in (obj.get("changes") or []) if isinstance(c, dict)]
        changed = {c.get("field"): c.get("value") for c in changes}
        if expect.get("no_changes") and changes:
            failures.append(f"changes: expected none, got {sorted(changed)!r}")
        only = expect.get("only_fields")
        if only is not None:
            extra = sorted(f for f in changed if f not in only)
            if extra:
                failures.append(f"changes: fields {extra!r} were not asked for")
        bad = sorted(str(f) for f in changed if f not in EDITABLE_FIELDS)
        if bad:
            failures.append(f"changes: fields {bad!r} are not editable")
        for field, want in (expect.get("field_values") or {}).items():
            if changed.get(field) != want:
                failures.append(f"{field}: expected {want!r}, got {changed.get(field)!r}")
        preset = obj.get("preset")
        allowed = expect.get("allowed_presets")
        if preset is not None and allowed is not None and preset not in allowed:
            failures.append(f"preset: {preset!r} is not one of {allowed!r}")
        if expect.get("critic"):
            failures += _over_limits({f: v for f, v in changed.items() if isinstance(v, str)}, EDIT_PROMPT_LIMITS)
            spec = data["spec"]
            new = apply_edit(spec, obj)
            failures += [f"critic: {p}" for p in check_spec(
                new, answers, extra_facts=[data["request"], *text_fields(spec).values()])]

    elif name == "listing_from_photo":
        allowed = expect.get("allowed_categories")
        if allowed is not None and obj.get("category") not in allowed:
            failures.append(f"category: {obj.get('category')!r} is not one of {allowed!r}")
        if expect.get("critic"):
            failures += _over_limits({k: obj.get(k) for k in LISTING_PROMPT_LIMITS
                                      if isinstance(obj.get(k), str)}, LISTING_PROMPT_LIMITS)
            cats = data.get("categories") or []
            failures += [f"critic: {p}" for p in check_listing(
                output, answers, seller_note=data.get("seller_note") or "", categories=cats or None)]

    return failures
