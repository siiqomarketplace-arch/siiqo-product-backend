"""
Tiny evaluation sets for gateway prompts.

A case file is JSON: ``evals/cases/<action>.v<N>.json``::

    {
      "action": "listing_parse@v1",
      "cases": [
        {
          "id": "naira-shorthand",
          "inputs": {"note": "..."},
          "expect": {
            "equals":           {"price_ngn": 15000},          # exact field values
            "required_keys":    ["title", "price_ngn"],         # must be present
            "non_empty":        ["title"],                      # truthy
            "max_length":       {"title": 80},                  # len(str) <= n
            "must_not_contain": ["verified", "escrow"],         # case-insensitive, whole output
            "contains_any":     {"title": ["ankara", "dress"]}  # field contains one of these
          }
        }
      ]
    }

``run_eval`` calls the real gateway with an injected client, so it runs
offline against a fake in tests. A live run (real client, real tokens, real
money, metered in ai_usage like any other call) is a deliberate manual step
before changing a prompt or model - never part of CI.

The onboarding prompts (store_spec, store_edit, listing_from_photo) use a
richer case format with a reference ``fixture`` and the code critic; see
``evals/onboarding.py``. Offline, ``tests/test_ai_evals.py`` checks every case
file's structure and runs each fixture through the critic.

Live run (paid; needs a database for the ai_usage rows, AI_ENABLED=true and
ANTHROPIC_API_KEY in the environment; run it against local or staging, never
from CI)::

    .venv/Scripts/python.exe -m app.services.ai.evals store_spec.v1 store_edit.v1 listing_from_photo.v1

With no names it runs every case file. It exits non-zero if any case fails.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from ..errors import AIError
from . import onboarding

CASES_DIR = Path(__file__).resolve().parent / "cases"


@dataclass
class CaseResult:
    case_id: str
    passed: bool
    failures: list[str] = field(default_factory=list)
    output: Any = None
    error: str | None = None
    skipped: bool = False


@dataclass
class EvalReport:
    action: str
    results: list[CaseResult]

    @property
    def passed(self) -> int:
        return sum(r.passed for r in self.results)

    @property
    def total(self) -> int:
        """Cases that ran (skipped cases are not counted)."""
        return sum(not r.skipped for r in self.results)

    @property
    def skipped(self) -> int:
        return sum(r.skipped for r in self.results)

    @property
    def pass_rate(self) -> float:
        return self.passed / self.total if self.total else 0.0

    def summary(self) -> str:
        head = f"{self.action}: {self.passed}/{self.total} passed"
        if self.skipped:
            head += f", {self.skipped} skipped"
        lines = [head]
        for r in self.results:
            if r.skipped:
                lines.append(f"  SKIP {r.case_id}: {r.error}")
            elif not r.passed:
                lines.append(f"  FAIL {r.case_id}: {r.error or '; '.join(r.failures)}")
        return "\n".join(lines)


def load_cases(path: str | Path) -> dict:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if "action" not in data or not isinstance(data.get("cases"), list):
        raise ValueError(f"{path}: an eval file needs 'action' and a 'cases' list")
    for case in data["cases"]:
        if "id" not in case or "inputs" not in case or "expect" not in case:
            raise ValueError(f"{path}: every case needs 'id', 'inputs' and 'expect'")
    ids = [c["id"] for c in data["cases"]]
    if len(set(ids)) != len(ids):
        raise ValueError(f"{path}: case ids must be unique")
    if onboarding.is_onboarding(data["action"]):
        problems = [p for c in data["cases"] for p in onboarding.validate_case(data["action"], c)]
        if problems:
            raise ValueError(f"{path}: " + "; ".join(problems[:10]))
    return data


def check(output: Any, expect: dict) -> list[str]:
    """Return the list of failed expectations (empty = pass)."""
    failures: list[str] = []
    obj = output if isinstance(output, dict) else {}
    blob = json.dumps(output, ensure_ascii=False).lower() if not isinstance(output, str) else output.lower()

    for key, want in (expect.get("equals") or {}).items():
        if obj.get(key) != want:
            failures.append(f"{key}: expected {want!r}, got {obj.get(key)!r}")
    for key in expect.get("required_keys") or []:
        if key not in obj:
            failures.append(f"{key}: missing")
    for key in expect.get("non_empty") or []:
        if not obj.get(key):
            failures.append(f"{key}: empty")
    for key, limit in (expect.get("max_length") or {}).items():
        value = obj.get(key)
        if value is not None and len(str(value)) > limit:
            failures.append(f"{key}: length {len(str(value))} > {limit}")
    for phrase in expect.get("must_not_contain") or []:
        if phrase.lower() in blob:
            failures.append(f"contains banned phrase {phrase!r}")
    for key, options in (expect.get("contains_any") or {}).items():
        value = str(obj.get(key) or "").lower()
        if not any(opt.lower() in value for opt in options):
            failures.append(f"{key}: contains none of {options!r}")
    return failures


def check_case(action: str, case: dict, output: Any) -> list[str]:
    """All failed expectations of one case: the generic ones plus, for the
    onboarding prompts, the critic, preset and edit checks."""
    failures = check(output, case.get("expect") or {})
    if onboarding.is_onboarding(action):
        failures += onboarding.check_onboarding(action, case, output)
    return failures


def run_eval(
    eval_set: dict | str | Path,
    *,
    client,
    seller_id: int | None = None,
    runner: Callable | None = None,
) -> EvalReport:
    """
    Run every case through the gateway with ``client`` injected and score the
    structured output (or text, for schema-less prompts). Gateway errors count
    as failures, they do not abort the run.
    """
    if not isinstance(eval_set, dict):
        eval_set = load_cases(eval_set)
    if runner is None:
        from ..gateway import run as runner

    action = eval_set["action"]
    results = []
    for case in eval_set["cases"]:
        try:
            inputs, images = onboarding.prepare(action, case)
        except onboarding.MissingImage as exc:
            results.append(CaseResult(case["id"], False, error=str(exc), skipped=True))
            continue
        kwargs = {"client": client}
        if images:
            kwargs["images"] = images
        try:
            result = runner(action, seller_id, inputs, **kwargs)
        except AIError as exc:
            results.append(CaseResult(case["id"], False, error=f"{type(exc).__name__}: {exc}"))
            continue
        output = result.data if result.data is not None else result.text
        failures = check_case(action, case, output)
        results.append(CaseResult(case["id"], not failures, failures, output))
    return EvalReport(eval_set["action"], results)
