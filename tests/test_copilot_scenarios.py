"""
Phase 4 (8b): seller copilot scenario evals, offline.

About 50 questions with known answers from a fixed fixture
(app/services/ai/evals/copilot.py), run through the real loop, tools and
action validation with a scripted fake model. The paid run with a real model
uses the same cases: see the module docstring for the command.
"""
import pytest

from app.services.ai.evals import copilot as evals

CASES = evals.load_cases()


def test_case_file_shape():
    ids = [c["id"] for c in CASES]
    assert len(ids) == len(set(ids)) and 45 <= len(ids) <= 60
    tools = {t["name"] for t in __import__("app.services.copilot_tools", fromlist=["TOOLS"]).TOOLS}
    for case in CASES:
        assert case["question"].strip()
        for turn in case.get("script") or []:
            for name, args in turn:
                assert name in tools, (case["id"], name)
                assert isinstance(args, dict)
        for t in (case.get("expect") or {}).get("tools_any") or []:
            assert t in tools


@pytest.fixture
def refs(monkeypatch):
    for key in ("AI_ENABLED_ACTIONS", "AI_SELLER_DAILY_ACTION_CAP", "AI_DAILY_COST_CAP_USD",
                "COPILOT_MONTHLY_CAP", "AI_COPILOT_MODEL"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("AI_ENABLED", "1")
    return evals.build_fixture()


def test_all_scenarios_offline(refs):
    results = evals.run_cases(refs, CASES, offline=True)
    failed = [(r.case_id, r.failures, r.answer) for r in results if not r.passed]
    assert not failed, failed
    assert len(results) == len(CASES)


def test_checks_catch_a_wrong_answer(refs):
    case = next(c for c in CASES if c["id"] == "unpaid-count")
    bad = dict(case, template="You have 7 unpaid orders.")
    result = evals.run_cases(refs, [bad], offline=True)[0]
    assert not result.passed and any("lacks" in f for f in result.failures)


def test_checks_catch_a_leak_and_bad_action(refs):
    case = next(c for c in CASES if c["id"] == "cross-seller-ship")
    leaky = dict(case, template="Bola Gadgets order shipped.",
                 actions=[{"type": "mark_shipped", "order_id": "$A.O4"}])
    result = evals.run_cases(refs, [leaky], offline=True)[0]
    assert not result.passed
    assert any("Bola" in f for f in result.failures) and any("actions" in f for f in result.failures)
