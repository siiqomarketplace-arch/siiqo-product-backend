"""
Live eval run (paid). Calls the real model through the gateway, so every
call is metered in ai_usage and counts against the AI caps.

    .venv/Scripts/python.exe -m app.services.ai.evals [store_spec.v1 ...]

Needs AI_ENABLED=true and ANTHROPIC_API_KEY, and a database the app can
write ai_usage rows to (local or staging). Never run from CI or tests:
tests/test_ai_evals.py covers everything that can be checked offline.
"""
from __future__ import annotations

import os
import sys

from . import CASES_DIR, run_eval


def main(argv: list[str]) -> int:
    if not os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("AI_ENABLED", "").lower() not in ("1", "true", "yes"):
        print("Set AI_ENABLED=true and ANTHROPIC_API_KEY to run the paid evaluation.", file=sys.stderr)
        return 2
    names = argv or sorted(p.stem for p in CASES_DIR.glob("*.json"))
    from app import create_app

    app = create_app()
    failed = 0
    with app.app_context():
        for name in names:
            path = CASES_DIR / f"{name.removesuffix('.json')}.json"
            if not path.is_file():
                print(f"No case file {path.name}", file=sys.stderr)
                failed += 1
                continue
            report = run_eval(path, client=None)
            print(report.summary())
            failed += report.total - report.passed
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
