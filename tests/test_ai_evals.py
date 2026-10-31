"""
Offline checks for the onboarding eval sets (Phase 1, item 1-10).

No model is called: every case file's structure is validated, and each
case's reference ``fixture`` is run through the same checks a live answer
gets (code critic, banned claims, fact trace, preset set, edit rules). The
paid run is ``python -m app.services.ai.evals`` (see evals/__init__.py).
"""
import copy
import json

import pytest

from app.services.ai import prompts
from app.services.ai.evals import CASES_DIR, check_case, load_cases, onboarding, run_eval
from app.services.store_spec import PRESET_IDS

ONBOARDING_FILES = ("store_spec.v1.json", "store_edit.v1.json", "listing_from_photo.v1.json")


def _cases(name):
    data = load_cases(CASES_DIR / name)
    return [(data["action"], c) for c in data["cases"]]


ALL_CASES = [pytest.param(a, c, id=f"{a}:{c['id']}") for name in ONBOARDING_FILES for a, c in _cases(name)]


def test_case_counts():
    counts = {name: len(_cases(name)) for name in ONBOARDING_FILES}
    assert 15 <= counts["store_spec.v1.json"] <= 30
    assert counts["store_edit.v1.json"] >= 10
    assert counts["listing_from_photo.v1.json"] >= 10


@pytest.mark.parametrize("name", ONBOARDING_FILES)
def test_case_files_target_registered_prompts(name):
    data = load_cases(CASES_DIR / name)
    spec = prompts.get(data["action"])
    assert f"{spec.action}.{spec.version}.json" == name


@pytest.mark.parametrize("action,case", ALL_CASES)
def test_fixture_passes_every_check(action, case):
    failures = check_case(action, case, case["fixture"])
    assert failures == [], failures


@pytest.mark.parametrize("action,case", ALL_CASES)
def test_case_states_the_required_checks(action, case):
    expect = case["expect"]
    assert expect["critic"] is True
    for word in ("verified", "escrow", "guarantee", "genuine"):
        assert word in expect["must_not_contain"]
    if onboarding.base_action(action) == "store_spec":
        assert set(expect["allowed_presets"]) <= set(PRESET_IDS)
    if onboarding.base_action(action) == "store_edit":
        assert set(expect["allowed_presets"]) <= set(PRESET_IDS)


def test_load_cases_rejects_bad_onboarding_cases(tmp_path):
    good = _cases("store_spec.v1.json")[0][1]
    for mutate in (
        lambda c: c.pop("fixture"),
        lambda c: c["expect"].pop("allowed_presets"),
        lambda c: c["expect"].update(critic=False),
        lambda c: c["inputs"].pop("answers"),
    ):
        bad = copy.deepcopy(good)
        mutate(bad)
        path = tmp_path / "bad.json"
        path.write_text(json.dumps({"action": "store_spec@v1", "cases": [bad]}), encoding="utf-8")
        with pytest.raises(ValueError):
            load_cases(path)

    photo = copy.deepcopy(_cases("listing_from_photo.v1.json")[0][1])
    photo["inputs"]["images"] = [{"path": "../secret.jpg", "shows": "x"}]
    path = tmp_path / "photo.json"
    path.write_text(json.dumps({"action": "listing_from_photo@v1", "cases": [photo]}), encoding="utf-8")
    with pytest.raises(ValueError):
        load_cases(path)


# ── The checks catch what they should ───────────────────────────────────────
@pytest.mark.parametrize("action,case", [p for p in ALL_CASES if p.values[0].startswith("store_spec")])
def test_critic_rejects_injected_claims_and_facts(action, case):
    claim = copy.deepcopy(case["fixture"])
    claim["hero"]["heading"] = "Verified store"
    assert any("banned claim 'verified'" in f for f in check_case(action, case, claim))

    invented = copy.deepcopy(case["fixture"])
    invented["copy"]["tagline"] = "Serving 9137 happy customers in Sokoto"
    failures = check_case(action, case, invented)
    assert any("9137" in f for f in failures)
    assert any("sokoto" in f for f in failures)

    wrong = copy.deepcopy(case["fixture"])
    wrong["preset"] = next(p for p in PRESET_IDS if p not in case["expect"]["allowed_presets"])
    assert any(f.startswith("preset:") for f in check_case(action, case, wrong))


def test_store_spec_prompt_limit_is_checked():
    action, case = _cases("store_spec.v1.json")[0]
    long = copy.deepcopy(case["fixture"])
    long["hero"]["heading"] = "Ankara " * 12
    assert any("prompt limit 60" in f for f in check_case(action, case, long))


def test_store_edit_checks():
    cases = dict((c["id"], (a, c)) for a, c in _cases("store_edit.v1.json"))
    action, case = cases["dictated-headline"]
    wrong_words = {"changes": [{"field": "hero.heading", "value": "Fresh bread daily"}], "preset": None, "summary": "x"}
    assert any("hero.heading: expected" in f for f in check_case(action, case, wrong_words))
    extra = {"changes": [{"field": "hero.heading", "value": "Fresh bread every morning"},
                         {"field": "copy.tagline", "value": "Bread"}], "preset": None, "summary": "x"}
    assert any("not asked for" in f for f in check_case(action, case, extra))

    action, case = cases["asks-for-verified-claim"]
    obeyed = {"changes": [{"field": "copy.tagline", "value": "Verified and guaranteed fresh"}],
              "preset": None, "summary": "Done."}
    failures = check_case(action, case, obeyed)
    assert any("expected none" in f for f in failures)
    assert any("banned claim" in f for f in failures)

    action, case = cases["darker-classier-look"]
    assert any(f.startswith("preset:") for f in check_case(
        action, case, {"changes": [], "preset": "playful-dynamic", "summary": "x"}))


def test_listing_checks():
    cases = dict((c["id"], (a, c)) for a, c in _cases("listing_from_photo.v1.json"))
    action, case = cases["white-sneakers-size-in-note"]
    priced = dict(case["fixture"], title="White sneakers ₦35,000")
    assert check_case(action, case, priced)
    branded = dict(case["fixture"], description="Nike sneakers, size 44.")
    failures = check_case(action, case, branded)
    assert any("nike" in f for f in failures) and any("44" in f for f in failures)
    assert any(f.startswith("category:") for f in check_case(action, case, dict(case["fixture"], category="Sports")))


# ── Input building and the runner, with a fake model ───────────────────────
def test_prepare_matches_production_inputs():
    action, case = next((a, c) for a, c in _cases("store_spec.v1.json") if c["id"] == "phone-number-not-in-copy")
    inputs, images = onboarding.prepare(action, case)
    assert images is None
    assert "contact_phone" not in inputs["answers"]           # never sent to the AI
    assert {p["id"] for p in inputs["presets"]} == set(PRESET_IDS)
    assert inputs["suggested_preset"] in PRESET_IDS


def test_listing_case_without_photo_is_skipped_not_passed(monkeypatch, tmp_path):
    monkeypatch.setattr(onboarding, "IMAGES_DIR", tmp_path)
    data = load_cases(CASES_DIR / "listing_from_photo.v1.json")
    calls = []
    report = run_eval(data, client=object(), runner=lambda *a, **k: calls.append(1))
    assert calls == []
    assert report.skipped == len(data["cases"]) and report.total == 0 and report.passed == 0
    assert "SKIP" in report.summary()


def test_runner_sends_photo_and_scores_with_critic(monkeypatch, tmp_path):
    from types import SimpleNamespace

    data = load_cases(CASES_DIR / "listing_from_photo.v1.json")
    case = data["cases"][0]
    photo = tmp_path / case["inputs"]["images"][0]["path"]
    photo.parent.mkdir(parents=True)
    photo.write_bytes(b"\xff\xd8\xff fake jpeg")
    monkeypatch.setattr(onboarding, "IMAGES_DIR", tmp_path)

    seen = {}

    def fake_runner(action, seller_id, inputs, *, client, images=None):
        seen["inputs"], seen["images"] = inputs, images
        return SimpleNamespace(data=case["fixture"], text="")

    report = run_eval({"action": data["action"], "cases": [case]}, client=object(), runner=fake_runner)
    assert report.passed == 1, report.summary()
    assert seen["images"][0]["type"] == "base64" and seen["images"][0]["media_type"] == "image/jpeg"
    assert seen["inputs"]["categories"] == case["inputs"]["categories"]

    bad = dict(case["fixture"], title="Verified genuine dress")
    report = run_eval({"action": data["action"], "cases": [case]}, client=object(),
                      runner=lambda *a, **k: SimpleNamespace(data=bad, text=""))
    assert report.passed == 0 and "banned claim" in report.summary()
