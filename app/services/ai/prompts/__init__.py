"""
Versioned prompt registry.

Prompts are data: one JSON file per version, named ``<action>.v<N>.json`` in
this directory. Changing a prompt means adding a new version file (and
re-running its eval set), never editing a version that has been used in
production - the version string is recorded on every ``ai_usage`` row.

File fields:
  action          str   action name the gateway is called with
  version         str   "v1", "v2", ...
  kind            str   "generation" (Opus) or "parsing" (Haiku)
  effort          str   output_config.effort: low | medium | high | xhigh | max
  max_tokens      int   output ceiling (includes adaptive thinking)
  seller_daily_cap int|null  optional per-seller daily cap for this action
  system          str   stable system prompt (cached; keep it byte-identical)
  user_template   str   user turn; "{inputs_json}" is replaced with the PII-stripped inputs
  output_schema   obj|null  JSON schema for structured output
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any

_DIR = Path(__file__).resolve().parent
_FILE_RE = re.compile(r"^(?P<action>[a-z][a-z0-9_]*)\.v(?P<num>\d+)\.json$")

KINDS = ("generation", "parsing")
EFFORTS = ("low", "medium", "high", "xhigh", "max")


@dataclass(frozen=True)
class PromptSpec:
    action: str
    version: str
    kind: str
    effort: str
    max_tokens: int
    system: str
    user_template: str
    output_schema: dict | None = None
    seller_daily_cap: int | None = None
    description: str = ""
    extra: dict = field(default_factory=dict)

    @property
    def ref(self) -> str:
        return f"{self.action}@{self.version}"

    def render_user(self, inputs_json: str) -> str:
        # Plain replace, not str.format: inputs may contain braces.
        return self.user_template.replace("{inputs_json}", inputs_json)


def _parse(path: Path) -> PromptSpec:
    data: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    m = _FILE_RE.match(path.name)
    if not m:
        raise ValueError(f"Bad prompt file name: {path.name}")
    if data.get("action") != m["action"] or data.get("version") != f"v{m['num']}":
        raise ValueError(f"{path.name}: action/version fields do not match the file name")
    if data.get("kind") not in KINDS:
        raise ValueError(f"{path.name}: kind must be one of {KINDS}")
    if data.get("effort") not in EFFORTS:
        raise ValueError(f"{path.name}: effort must be one of {EFFORTS}")
    if "{inputs_json}" not in data.get("user_template", ""):
        raise ValueError(f"{path.name}: user_template must contain {{inputs_json}}")
    known = {f for f in PromptSpec.__dataclass_fields__ if f != "extra"}
    return PromptSpec(
        **{k: v for k, v in data.items() if k in known},
        extra={k: v for k, v in data.items() if k not in known},
    )


@lru_cache(maxsize=1)
def load_all() -> dict[str, dict[int, PromptSpec]]:
    """{action: {version_number: PromptSpec}} for every prompt file in this directory."""
    registry: dict[str, dict[int, PromptSpec]] = {}
    for path in sorted(_DIR.glob("*.json")):
        m = _FILE_RE.match(path.name)
        if not m:
            continue
        registry.setdefault(m["action"], {})[int(m["num"])] = _parse(path)
    return registry


def get(ref: str) -> PromptSpec:
    """
    Resolve ``"action"`` (latest version) or ``"action@vN"`` (pinned).
    Raises KeyError for an unknown action or version.
    """
    action, _, version = ref.partition("@")
    versions = load_all().get(action)
    if not versions:
        raise KeyError(f"Unknown AI action: {action!r}")
    if not version:
        return versions[max(versions)]
    if not re.fullmatch(r"v\d+", version) or int(version[1:]) not in versions:
        raise KeyError(f"Unknown version {version!r} for AI action {action!r}")
    return versions[int(version[1:])]


def actions() -> list[str]:
    return sorted(load_all())
