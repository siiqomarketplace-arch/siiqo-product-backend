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
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from ..errors import AIError

CASES_DIR = Path(__file__).resolve().parent / "cases"


@dataclass
class CaseResult:
    case_id: str
    passed: bool
    failures: list[str] = field(default_factory=list)
    output: Any = None
    error: str | None = None


@dataclass
class EvalReport:
    action: str
    results: list[CaseResult]

    @property
    def passed(self) -> int:
        return sum(r.passed for r in self.results)

    @property
    def total(self) -> int:
        return len(self.results)

    @property
    def pass_rate(self) -> float:
        return self.passed / self.total if self.total else 0.0

    def summary(self) -> str:
        lines = [f"{self.action}: {self.passed}/{self.total} passed"]
        for r in self.results:
            if not r.passed:
                lines.append(f"  FAIL {r.case_id}: {r.error or '; '.join(r.failures)}")
        return "\n".join(lines)


def load_cases(path: str | Path) -> dict:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if "action" not in data or not isinstance(data.get("cases"), list):
        raise ValueError(f"{path}: an eval file needs 'action' and a 'cases' list")
    for case in data["cases"]:
        if "id" not in case or "inputs" not in case or "expect" not in case:
            raise ValueError(f"{path}: every case needs 'id', 'inputs' and 'expect'")
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

    results = []
    for case in eval_set["cases"]:
        try:
            result = runner(eval_set["action"], seller_id, case["inputs"], client=client)
        except AIError as exc:
            results.append(CaseResult(case["id"], False, error=f"{type(exc).__name__}: {exc}"))
            continue
        output = result.data if result.data is not None else result.text
        failures = check(output, case["expect"])
        results.append(CaseResult(case["id"], not failures, failures, output))
    return EvalReport(eval_set["action"], results)
