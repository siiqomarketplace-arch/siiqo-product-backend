"""
Siiqo AI gateway (Phase 0B) - the only place the backend talks to Claude.

    from app.services.ai import run, AIError

    try:
        result = run("listing_parse", seller_id=vendor.id, inputs={"note": text})
        fields = result.data            # dict, schema-validated
    except AIError:
        fields = None                   # fall back to the non-AI path

Every call, in order:
  1. resolve the versioned prompt ("action" or "action@vN");
  2. kill switch: env AI_ENABLED, optional env allowlist, PlatformSetting overrides;
  3. strip personal data from the inputs;
  4. caps: optional global daily cost cap (off unless AI_DAILY_COST_CAP_USD > 0), per-seller daily action caps (from ai_usage);
  5. pick the model by prompt kind (generation -> Opus 5.5, parsing -> Haiku 5.5);
  6. call Claude with the system prompt marked for prompt caching, effort set
     explicitly, structured output when a schema applies, and (Opus only) the
     server-side refusal fallback;
  7. check stop_reason before reading content;
  8. write one ai_usage row and return an AIResult, or raise a typed AIError.

The ``anthropic`` package is imported lazily, only when no client is injected,
so the app and the tests run without it installed.
"""
from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import func
from sqlalchemy.exc import SQLAlchemyError

from app.extensions import db
from app.models.admin import PlatformSetting
from app.models.ai import AIUsage

from . import pii, prompts
from .errors import (
    AICapExceeded,
    AIDisabled,
    AIError,
    AIInvalidOutput,
    AIRefused,
    AIRequestError,
    AIUnavailable,
)
from .pricing import estimate_microusd, price_for

log = logging.getLogger(__name__)

# ── Models ──────────────────────────────────────────────────────────────────
MODELS = {
    "generation": "claude-opus-5-5",
    "parsing": "claude-haiku-5-5",
}

# Server-side refusal fallback. Opus 5.5 supports it; Haiku 5.5 has no
# server-side fallback (claude-api skill, shared/models.md). The scalar
# "default" form must be paired with exactly this beta header - the array
# form uses a different one and mixing them is a 400.
FALLBACK_BETA = "server-side-fallback-2026-07-01"
SERVER_FALLBACK_MODELS = frozenset({"claude-opus-5-5"})

# ── PlatformSetting keys (runtime overrides, no deploy needed) ──────────────
SETTING_ENABLED = "ai_enabled"                    # "false"/"0"/"off" turns all AI off
SETTING_DISABLED_ACTIONS = "ai_disabled_actions"  # comma list of actions to turn off

# Outcomes that reached the model and may have cost money; caps count these.
BILLABLE_OUTCOMES = ("ok", "refused", "invalid_output", "truncated")

_TRUE = {"1", "true", "yes", "on"}
_FALSE = {"0", "false", "no", "off"}


# ── Configuration (env only) ────────────────────────────────────────────────
def _env_bool(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    return raw.strip().lower() in _TRUE


def _env_number(name: str, default, cast):
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return cast(raw)
    except ValueError:
        log.warning("AI gateway: invalid %s=%r, using default %r", name, raw, default)
        return default


def _env_list(name: str) -> set[str]:
    raw = os.environ.get(name) or ""
    return {part.strip() for part in raw.split(",") if part.strip()}


@dataclass(frozen=True)
class GatewayConfig:
    enabled: bool
    allowed_actions: frozenset          # empty = every registered action
    seller_daily_action_cap: int
    daily_cost_cap_usd: float
    max_input_chars: int
    timeout_seconds: float
    max_retries: int

    @classmethod
    def from_env(cls) -> "GatewayConfig":
        return cls(
            enabled=_env_bool("AI_ENABLED", False),
            allowed_actions=frozenset(_env_list("AI_ENABLED_ACTIONS")),
            seller_daily_action_cap=_env_number("AI_SELLER_DAILY_ACTION_CAP", 50, int),
            # 0 = no global cap (founder decision, 30 Oct 2026); spend is still metered.
            daily_cost_cap_usd=_env_number("AI_DAILY_COST_CAP_USD", 0.0, float),
            max_input_chars=_env_number("AI_MAX_INPUT_CHARS", 20_000, int),
            timeout_seconds=_env_number("AI_TIMEOUT_SECONDS", 60.0, float),
            max_retries=_env_number("AI_MAX_RETRIES", 2, int),
        )


# ── Result ──────────────────────────────────────────────────────────────────
@dataclass
class AIResult:
    action: str
    prompt_version: str
    model: str                  # requested
    served_model: str           # answered (differs after a server-side fallback)
    text: str                   # concatenated text blocks
    data: Any = None            # parsed JSON when a schema was used
    stop_reason: str | None = None
    fell_back: bool = False
    input_tokens: int = 0
    output_tokens: int = 0
    cache_creation_input_tokens: int = 0
    cache_read_input_tokens: int = 0
    estimated_cost_usd: float = 0.0
    latency_ms: int = 0
    pii_redactions: int = 0
    request_id: str | None = None
    usage_id: int | None = None
    meta: dict = field(default_factory=dict)


# ── Kill switch ─────────────────────────────────────────────────────────────
def kill_switch_state(action: str, config: GatewayConfig | None = None) -> tuple[bool, str]:
    """
    (enabled, reason). AI runs only if env AI_ENABLED is on AND no runtime
    PlatformSetting turns it off. The runtime setting can only switch AI off,
    never on: enabling spend requires a deploy-time env change.
    Database errors propagate (the caller fails closed).
    """
    config = config or GatewayConfig.from_env()
    if not config.enabled:
        return False, "AI_ENABLED is off"
    if config.allowed_actions and action not in config.allowed_actions:
        return False, f"action {action!r} not in AI_ENABLED_ACTIONS"

    rows = PlatformSetting.query.filter(
        PlatformSetting.key.in_([SETTING_ENABLED, SETTING_DISABLED_ACTIONS])
    ).all()
    settings = {r.key: (r.value or "").strip() for r in rows}
    if settings.get(SETTING_ENABLED, "").lower() in _FALSE:
        return False, f"PlatformSetting {SETTING_ENABLED} is off"
    disabled = {a.strip() for a in settings.get(SETTING_DISABLED_ACTIONS, "").split(",") if a.strip()}
    if action in disabled or "*" in disabled:
        return False, f"action {action!r} disabled by PlatformSetting {SETTING_DISABLED_ACTIONS}"
    return True, "enabled"


def set_runtime_kill_switch(enabled: bool) -> None:
    """Admin helper: flip the runtime override. Commits the session."""
    row = PlatformSetting.query.filter_by(key=SETTING_ENABLED).first()
    if row is None:
        row = PlatformSetting(key=SETTING_ENABLED, value="", description="AI gateway runtime kill switch")
        db.session.add(row)
    row.value = "true" if enabled else "false"
    db.session.commit()


# ── Metering ────────────────────────────────────────────────────────────────
def _day_start(now: datetime | None = None) -> datetime:
    now = now or datetime.now(timezone.utc)
    return now.replace(hour=0, minute=0, second=0, microsecond=0)


def global_cost_today_usd() -> float:
    total = (
        db.session.query(func.coalesce(func.sum(AIUsage.estimated_cost_microusd), 0))
        .filter(AIUsage.created_at >= _day_start())
        .scalar()
    )
    return int(total or 0) / 1_000_000


def seller_actions_today(seller_id: int, action: str | None = None) -> int:
    q = AIUsage.query.filter(
        AIUsage.seller_id == seller_id,
        AIUsage.created_at >= _day_start(),
        AIUsage.outcome.in_(BILLABLE_OUTCOMES),
    )
    if action:
        q = q.filter(AIUsage.action == action)
    return q.count()


def _record(**fields) -> int | None:
    """
    Insert one ai_usage row on its own connection and transaction, so the
    caller's pending session changes are never committed as a side effect.
    Metering failures are logged, never raised.
    """
    fields.setdefault("created_at", datetime.now(timezone.utc))
    for name, limit in (("error_type", 64), ("stop_reason", 32), ("request_id", 128),
                        ("served_model", 64)):
        if isinstance(fields.get(name), str):
            fields[name] = fields[name][:limit]
    try:
        with db.engine.begin() as conn:
            res = conn.execute(AIUsage.__table__.insert().values(**fields))
            pk = res.inserted_primary_key
            return pk[0] if pk else None
    except SQLAlchemyError:
        log.exception("AI gateway: failed to write ai_usage row (action=%s)", fields.get("action"))
        return None


# ── Client ──────────────────────────────────────────────────────────────────
_client_cache: dict[str, Any] = {}


def _default_client(config: GatewayConfig):
    api_key = (os.environ.get("ANTHROPIC_API_KEY") or "").strip()
    if not api_key:
        raise AIUnavailable("ANTHROPIC_API_KEY is not set", reason="no_api_key")
    try:
        import anthropic  # lazy: the package is optional until AI is switched on
    except ImportError as exc:
        raise AIUnavailable("the anthropic package is not installed", reason="sdk_missing") from exc
    cache_key = f"{id(anthropic)}:{hash(api_key)}:{config.timeout_seconds}:{config.max_retries}"
    client = _client_cache.get(cache_key)
    if client is None:
        _client_cache.clear()
        client = anthropic.Anthropic(
            api_key=api_key, timeout=config.timeout_seconds, max_retries=config.max_retries
        )
        _client_cache[cache_key] = client
    return client


class _NeverRaised(Exception):
    """Stand-in for SDK error classes when the anthropic package is absent."""


class _SDKErrors:
    """The SDK's typed errors, or never-matching stand-ins when it is not installed."""

    def __init__(self):
        try:
            import anthropic as sdk
        except ImportError:
            sdk = None
        for name in (
            "APITimeoutError", "APIConnectionError", "AuthenticationError",
            "PermissionDeniedError", "RateLimitError", "BadRequestError", "NotFoundError",
            "UnprocessableEntityError", "InternalServerError", "APIStatusError", "APIError",
        ):
            setattr(self, name, getattr(sdk, name, _NeverRaised) if sdk else _NeverRaised)


def _call_claude(client, *, model: str, request: dict):
    """Send the request; translate SDK errors (most specific first) into gateway errors."""
    E = _SDKErrors()
    try:
        if model in SERVER_FALLBACK_MODELS:
            # fallbacks="default" goes through extra_body so it works on every
            # anthropic 1.x release, including ones whose typed signature
            # predates the scalar form.
            return client.beta.messages.create(
                **request, betas=[FALLBACK_BETA], extra_body={"fallbacks": "default"}
            )
        return client.messages.create(**request)
    except E.APITimeoutError as exc:          # subclass of APIConnectionError
        raise AIUnavailable("Claude API timed out", reason="timeout") from exc
    except E.APIConnectionError as exc:
        raise AIUnavailable("could not reach the Claude API", reason="connection") from exc
    except E.AuthenticationError as exc:
        raise AIUnavailable("Claude API key rejected", reason="auth") from exc
    except E.PermissionDeniedError as exc:
        raise AIUnavailable("Claude API key lacks permission", reason="permission") from exc
    except E.RateLimitError as exc:
        raise AIUnavailable("Claude API rate limit", reason="rate_limited") from exc
    except (E.BadRequestError, E.NotFoundError, E.UnprocessableEntityError) as exc:
        raise AIRequestError(f"Claude API rejected the request: {getattr(exc, 'message', exc)}",
                             reason="bad_request") from exc
    except E.InternalServerError as exc:
        raise AIUnavailable("Claude API server error", reason="server_error") from exc
    except E.APIStatusError as exc:           # other 4xx/5xx, including 529 overloaded
        status = getattr(exc, "status_code", "unknown")
        raise AIUnavailable(f"Claude API error {status}", reason=f"status_{status}") from exc
    except E.APIError as exc:
        raise AIUnavailable("Claude API error", reason="api_error") from exc
    except Exception as exc:  # noqa: BLE001 - a broken client must not take the request down
        log.exception("AI gateway: unexpected error calling Claude")
        raise AIUnavailable("unexpected error calling Claude", reason="unexpected") from exc


# ── Response helpers ────────────────────────────────────────────────────────
def _get(obj, name: str, default=None):
    """Attribute access that also works on plain dicts (fake clients, extra fields)."""
    if obj is None:
        return default
    if isinstance(obj, dict):
        return obj.get(name, default)
    return getattr(obj, name, default)


def _usage_totals(response, requested_model: str) -> dict:
    """Token totals and estimated cost. Sums usage.iterations when present
    (one entry per attempt, including declined ones before a fallback)."""
    usage = _get(response, "usage")
    served = _get(response, "model") or requested_model
    keys = ("input_tokens", "output_tokens", "cache_creation_input_tokens", "cache_read_input_tokens")

    def tokens(entry) -> dict:
        return {k: int(_get(entry, k, 0) or 0) for k in keys}

    iterations = list(_get(usage, "iterations", None) or [])
    totals = dict.fromkeys(keys, 0)
    cost = 0
    if iterations:
        # Price an attempt whose model is not reported at the dearer of the two.
        dearer = max((requested_model, served), key=lambda m: price_for(m).output)
        for it in iterations:
            t = tokens(it)
            for k in keys:
                totals[k] += t[k]
            cost += estimate_microusd(_get(it, "model") or dearer, **t)
    else:
        totals = tokens(usage)
        cost = estimate_microusd(served, **totals)
    totals["estimated_cost_microusd"] = cost
    return totals


def _fell_back(response) -> bool:
    if any(_get(b, "type") == "fallback" for b in (_get(response, "content") or [])):
        return True
    iterations = _get(_get(response, "usage"), "iterations", None) or []
    return any(_get(it, "type") == "fallback_message" for it in iterations)


def _text_blocks(response) -> list[str]:
    return [_get(b, "text") or "" for b in (_get(response, "content") or []) if _get(b, "type") == "text"]


_JSON_TYPES = {
    "object": dict, "array": list, "string": str, "boolean": bool, "null": type(None),
}


def _schema_errors(value, schema: dict, path: str = "$") -> list[str]:
    """Small JSON-schema subset check (type, properties, required,
    additionalProperties, items, anyOf, enum). The API already enforces the
    schema; this is a cheap guard against drift and fake clients."""
    if not isinstance(schema, dict):
        return []
    if "anyOf" in schema:
        if any(not _schema_errors(value, s, path) for s in schema["anyOf"]):
            return []
        return [f"{path}: matches no anyOf branch"]
    if "enum" in schema and value not in schema["enum"]:
        return [f"{path}: not in enum"]
    t = schema.get("type")
    if t:
        types = t if isinstance(t, list) else [t]
        ok = False
        for name in types:
            if name == "integer":
                ok |= isinstance(value, int) and not isinstance(value, bool)
            elif name == "number":
                ok |= isinstance(value, (int, float)) and not isinstance(value, bool)
            elif name in _JSON_TYPES:
                ok |= isinstance(value, _JSON_TYPES[name]) and not (
                    name != "boolean" and isinstance(value, bool)
                )
        if not ok:
            return [f"{path}: expected {t}"]
    errors: list[str] = []
    if isinstance(value, dict):
        props = schema.get("properties", {})
        for key in schema.get("required", []):
            if key not in value:
                errors.append(f"{path}.{key}: missing")
        if schema.get("additionalProperties") is False:
            errors += [f"{path}.{k}: unexpected" for k in value if k not in props]
        for k, sub in props.items():
            if k in value:
                errors += _schema_errors(value[k], sub, f"{path}.{k}")
    if isinstance(value, list) and "items" in schema:
        for i, item in enumerate(value):
            errors += _schema_errors(item, schema["items"], f"{path}[{i}]")
    return errors


# ── Entry point ─────────────────────────────────────────────────────────────
def run(
    action: str,
    seller_id: int | None,
    inputs: dict,
    *,
    schema: dict | None = None,
    client=None,
    images: list[dict] | None = None,
) -> AIResult:
    """
    Run one AI action. ``action`` is "name" (latest prompt version) or
    "name@vN". ``schema`` overrides the prompt's output_schema; when either
    is set the result's ``data`` holds the validated JSON. ``client`` injects
    an Anthropic-compatible client (tests, evals); by default one is built
    from ANTHROPIC_API_KEY. ``images`` (vision prompts) is a list of
    Messages API image sources - ``{"type": "base64", "media_type": ...,
    "data": ...}`` or ``{"type": "url", "url": ...}`` - sent before the text
    in the user turn. Only product photos and logos may be passed here,
    never ID documents (the PII stripper cannot see inside images).

    Raises AIDisabled, AICapExceeded, AIRefused, AIUnavailable,
    AIInvalidOutput or AIRequestError (all subclasses of AIError).

    Call it with no uncommitted writes in the session: a model call takes
    seconds, and the usage row is written on its own connection (so the
    caller's pending changes are never committed by the gateway).
    """
    config = GatewayConfig.from_env()

    # 1. Prompt
    try:
        spec = prompts.get(action)
    except KeyError as exc:
        raise AIRequestError(str(exc), reason="unknown_action") from exc

    # 2. Kill switch (fail closed if the settings table cannot be read)
    try:
        enabled, why = kill_switch_state(spec.action, config)
    except SQLAlchemyError as exc:
        db.session.rollback()
        raise AIUnavailable("could not read AI settings", reason="settings_unreadable") from exc
    if not enabled:
        raise AIDisabled(f"AI is disabled: {why}", reason=why)

    model = MODELS[spec.kind]
    output_schema = schema if schema is not None else spec.output_schema
    row = {
        "action": spec.action,
        "prompt_version": spec.version,
        "model": model,
        "seller_id": seller_id,
    }

    # 3. Personal data out
    if not isinstance(inputs, dict):
        raise AIRequestError("inputs must be a dict", reason="bad_inputs")
    clean_inputs, redactions = pii.strip(inputs)
    row["pii_redactions"] = redactions
    inputs_json = json.dumps(clean_inputs, ensure_ascii=False, sort_keys=True, default=str)
    if len(inputs_json) > config.max_input_chars:
        raise AIRequestError(
            f"inputs too large ({len(inputs_json)} > {config.max_input_chars} chars)",
            reason="input_too_large",
        )

    # 4. Caps (fail closed if metering cannot be read)
    try:
        spent = global_cost_today_usd()
        if config.daily_cost_cap_usd > 0 and spent >= config.daily_cost_cap_usd:
            _record(**row, outcome="cap_exceeded", error_type="global_cost_cap")
            raise AICapExceeded(
                f"global daily AI cost cap reached (${spent:.2f} of ${config.daily_cost_cap_usd:.2f})",
                reason="global_cost_cap",
            )
        if seller_id is not None:
            if seller_actions_today(seller_id) >= config.seller_daily_action_cap:
                _record(**row, outcome="cap_exceeded", error_type="seller_daily_cap")
                raise AICapExceeded("seller daily AI action cap reached", reason="seller_daily_cap")
            if spec.seller_daily_cap is not None and \
                    seller_actions_today(seller_id, spec.action) >= spec.seller_daily_cap:
                _record(**row, outcome="cap_exceeded", error_type="seller_action_cap")
                raise AICapExceeded(
                    f"seller daily cap for {spec.action!r} reached", reason="seller_action_cap"
                )
    except SQLAlchemyError as exc:
        db.session.rollback()
        raise AIUnavailable("could not read AI usage", reason="metering_unreadable") from exc

    # 5. Client
    if client is None:
        try:
            client = _default_client(config)
        except AIUnavailable as exc:
            _record(**row, outcome="unavailable", error_type=exc.reason)
            raise

    # 6. Request. The system prompt is stable and carries the cache marker;
    #    volatile inputs live only in the user turn. Thinking is always on for
    #    these models, so no `thinking` field; effort is set explicitly.
    output_config: dict = {"effort": spec.effort}
    if output_schema:
        output_config["format"] = {"type": "json_schema", "schema": output_schema}
    user_content: Any = spec.render_user(inputs_json)
    if images:
        blocks = []
        for source in images:
            if not isinstance(source, dict) or source.get("type") not in ("base64", "url"):
                raise AIRequestError("images must be base64 or url image sources", reason="bad_inputs")
            blocks.append({"type": "image", "source": source})
        user_content = [*blocks, {"type": "text", "text": user_content}]
    request = {
        "model": model,
        "max_tokens": spec.max_tokens,
        "system": [{"type": "text", "text": spec.system, "cache_control": {"type": "ephemeral"}}],
        "messages": [{"role": "user", "content": user_content}],
        "output_config": output_config,
    }

    started = time.monotonic()
    try:
        response = _call_claude(client, model=model, request=request)
    except AIError as exc:
        _record(**row, outcome="provider_error", error_type=exc.reason,
                latency_ms=int((time.monotonic() - started) * 1000))
        raise
    latency_ms = int((time.monotonic() - started) * 1000)

    # 7. Usage first (a refusal can still cost tokens), then stop_reason.
    totals = _usage_totals(response, model)
    stop_reason = _get(response, "stop_reason")
    meter = {
        **row,
        **totals,
        "served_model": _get(response, "model") or model,
        "latency_ms": latency_ms,
        "stop_reason": stop_reason,
        "fell_back": _fell_back(response),
        "request_id": _get(response, "_request_id") or _get(response, "id"),
    }

    if stop_reason == "refusal":
        category = _get(_get(response, "stop_details"), "category")
        _record(**meter, outcome="refused", error_type=category or "refusal")
        raise AIRefused(f"Claude declined the {spec.action!r} request", category=category)
    if stop_reason == "max_tokens":
        _record(**meter, outcome="truncated", error_type="max_tokens")
        raise AIInvalidOutput("response hit max_tokens before finishing", reason="max_tokens")

    texts = _text_blocks(response)
    text = "".join(texts)
    data = None
    if output_schema:
        try:
            # output_config.format guarantees the first text block is the JSON.
            data = json.loads(texts[0]) if texts else None
        except (json.JSONDecodeError, TypeError):
            data = None
        problems = ["no JSON object"] if data is None else _schema_errors(data, output_schema)
        if problems:
            _record(**meter, outcome="invalid_output", error_type="schema_mismatch")
            raise AIInvalidOutput(
                f"output did not match the schema: {'; '.join(problems[:5])}",
                reason="schema_mismatch",
            )
    elif not text.strip():
        _record(**meter, outcome="invalid_output", error_type="empty")
        raise AIInvalidOutput("empty response", reason="empty")

    usage_id = _record(**meter, outcome="ok")
    return AIResult(
        action=spec.action,
        prompt_version=spec.version,
        model=model,
        served_model=meter["served_model"],
        text=text,
        data=data,
        stop_reason=stop_reason,
        fell_back=meter["fell_back"],
        input_tokens=totals["input_tokens"],
        output_tokens=totals["output_tokens"],
        cache_creation_input_tokens=totals["cache_creation_input_tokens"],
        cache_read_input_tokens=totals["cache_read_input_tokens"],
        estimated_cost_usd=totals["estimated_cost_microusd"] / 1_000_000,
        latency_ms=latency_ms,
        pii_redactions=redactions,
        request_id=meter["request_id"],
        usage_id=usage_id,
    )
