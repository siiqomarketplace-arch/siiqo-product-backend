"""
AI gateway (Phase 0B). Every test uses a fake Anthropic client: no network,
no API key, and the `anthropic` package does not need to be installed.
"""
import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from app.extensions import db
from app.models.admin import PlatformSetting
from app.models.ai import AIUsage
from app.services.ai import (
    AICapExceeded,
    AIDisabled,
    AIInvalidOutput,
    AIRefused,
    AIRequestError,
    AIUnavailable,
    gateway,
    pii,
    prompts,
    run,
    set_runtime_kill_switch,
)
from app.services.ai.evals import CASES_DIR, check, load_cases, run_eval
from app.services.ai.pricing import estimate_microusd

_AI_ENV = [
    "ANTHROPIC_API_KEY", "AI_ENABLED", "AI_ENABLED_ACTIONS", "AI_SELLER_DAILY_ACTION_CAP",
    "AI_DAILY_COST_CAP_USD", "AI_MAX_INPUT_CHARS", "AI_TIMEOUT_SECONDS", "AI_MAX_RETRIES",
]

LISTING = {"title": "Ankara maxi dress", "price_ngn": 15000, "quantity": 3, "attributes": []}
STORE = {"headline": "Made-to-measure Ankara", "description": "Dresses and tops.", "tags": ["ankara"]}


# ── Fakes ───────────────────────────────────────────────────────────────────
def make_response(text="ok", *, stop_reason="end_turn", model="claude-haiku-5-5",
                  input_tokens=1000, output_tokens=200, cache_creation=0, cache_read=0,
                  iterations=None, content=None, stop_details=None):
    usage = SimpleNamespace(
        input_tokens=input_tokens, output_tokens=output_tokens,
        cache_creation_input_tokens=cache_creation, cache_read_input_tokens=cache_read,
        iterations=iterations,
    )
    if content is None:
        content = [SimpleNamespace(type="thinking", thinking=""), SimpleNamespace(type="text", text=text)]
    return SimpleNamespace(id="msg_test", model=model, stop_reason=stop_reason,
                           stop_details=stop_details, content=content, usage=usage)


class _Endpoint:
    def __init__(self, owner, route):
        self._owner, self._route = owner, route

    def create(self, **kwargs):
        self._owner.calls.append((self._route, kwargs))
        if self._owner.error is not None:
            raise self._owner.error
        item = self._owner.responses.pop(0) if len(self._owner.responses) > 1 else self._owner.responses[0]
        return item(kwargs) if callable(item) else item


class FakeClient:
    """Anthropic-shaped: client.messages.create and client.beta.messages.create."""

    def __init__(self, *responses, error=None):
        self.responses = list(responses) or [make_response()]
        self.error = error
        self.calls = []
        self.messages = _Endpoint(self, "messages")
        self.beta = SimpleNamespace(messages=_Endpoint(self, "beta"))


def json_response(obj, **kw):
    return make_response(json.dumps(obj), **kw)


# ── Fixtures ────────────────────────────────────────────────────────────────
@pytest.fixture(autouse=True)
def ai_env(monkeypatch):
    for key in _AI_ENV:
        monkeypatch.delenv(key, raising=False)
    return monkeypatch


@pytest.fixture
def enabled(ai_env):
    ai_env.setenv("AI_ENABLED", "1")


@pytest.fixture
def seller(make_user):
    return make_user()


def rows():
    db.session.expire_all()
    return AIUsage.query.order_by(AIUsage.id).all()


# ── Kill switch ─────────────────────────────────────────────────────────────
def test_disabled_by_default_and_client_never_called():
    client = FakeClient(json_response(LISTING))
    with pytest.raises(AIDisabled):
        run("listing_parse", None, {"note": "x"}, client=client)
    assert client.calls == []
    assert rows() == []


@pytest.mark.parametrize("value", ["0", "false", "off", "no", "garbage"])
def test_env_values_that_do_not_enable(ai_env, value):
    ai_env.setenv("AI_ENABLED", value)
    with pytest.raises(AIDisabled):
        run("listing_parse", None, {"note": "x"}, client=FakeClient())


def test_runtime_kill_switch_overrides_env(enabled):
    client = FakeClient(json_response(LISTING))
    assert run("listing_parse", None, {"note": "x"}, client=client).data == LISTING

    set_runtime_kill_switch(False)
    with pytest.raises(AIDisabled):
        run("listing_parse", None, {"note": "x"}, client=client)
    assert len(client.calls) == 1

    set_runtime_kill_switch(True)
    assert run("listing_parse", None, {"note": "x"}, client=client).data == LISTING


def test_runtime_setting_cannot_enable_when_env_is_off():
    db.session.add(PlatformSetting(key="ai_enabled", value="true"))
    db.session.commit()
    with pytest.raises(AIDisabled):
        run("listing_parse", None, {"note": "x"}, client=FakeClient())


def test_per_action_disable_and_allowlist(enabled, ai_env):
    db.session.add(PlatformSetting(key="ai_disabled_actions", value="store_description"))
    db.session.commit()
    with pytest.raises(AIDisabled):
        run("store_description", None, {"business_name": "x"}, client=FakeClient(json_response(STORE)))
    assert run("listing_parse", None, {"note": "x"}, client=FakeClient(json_response(LISTING))).data

    ai_env.setenv("AI_ENABLED_ACTIONS", "store_description")
    with pytest.raises(AIDisabled):
        run("listing_parse", None, {"note": "x"}, client=FakeClient(json_response(LISTING)))


# ── Personal data ───────────────────────────────────────────────────────────
@pytest.mark.parametrize("raw, expected", [
    ("mail me at ada.obi+shop@gmail.com now", "mail me at [email] now"),
    ("call 08031234567", "call [phone]"),
    ("call 0803 123 4567", "call [phone]"),
    ("call 0803-123-4567", "call [phone]"),
    ("call +2348031234567", "call [phone]"),
    ("call +234 803 123 4567", "call [phone]"),
    ("call 234 (0) 803 123 4567", "call [phone]"),
    ("whatsapp 09123456789 or 07012345678", "whatsapp [phone] or [phone]"),
    ("acct:0123456789 GTB", "acct:[account_number] GTB"),
    ("my BVN is 22123456789", "my BVN is [id_number]"),
    ("NIN 12345678901.", "NIN [id_number]."),
    ("card 4111 1111 1111 1111 exp", "card [card_number] exp"),
    ("card 4111-1111-1111-1111", "card [card_number]"),
    ("card 5399831234567890123", "card [card_number]"),
])
def test_pii_masking(raw, expected):
    assert pii.strip_text(raw)[0] == expected


@pytest.mark.parametrize("safe", [
    "Price 15000 naira, 3 left",
    "N250,000 for two",
    "size 12, 500g tub, model 2024",
    "order #123456789",           # 9 digits
    "SKU 123456789012",           # 12 digits: not an account, NIN or card
])
def test_pii_leaves_ordinary_numbers(safe):
    assert pii.strip_text(safe) == (safe, 0)


def test_pii_nested_structures_and_sensitive_keys():
    clean, n = pii.strip({
        "note": "reach me on 08031234567",
        "phone": "anything at all",
        "bank_account_number": "0123456789",
        "items": [{"desc": "email x@y.co"}, "plain"],
        "price": 15000,
        "bvn": None,
    })
    assert clean == {
        "note": "reach me on [phone]",
        "phone": "[redacted]",
        "bank_account_number": "[redacted]",
        "items": [{"desc": "email [email]"}, "plain"],
        "price": 15000,
        "bvn": None,
    }
    assert n == 4


def test_gateway_sends_only_stripped_inputs(enabled):
    client = FakeClient(json_response(LISTING))
    result = run("listing_parse", None, {
        "note": "Shea butter 4500. Call 08031234567, pay 0123456789, ada@example.com",
    }, client=client)
    sent = json.dumps(client.calls[0][1])
    for secret in ("08031234567", "0123456789", "ada@example.com"):
        assert secret not in sent
    assert "[phone]" in sent and "[account_number]" in sent and "[email]" in sent
    assert result.pii_redactions == 3
    assert rows()[0].pii_redactions == 3


# ── Request shape ───────────────────────────────────────────────────────────
def test_parsing_uses_haiku_with_structured_output_and_cached_system(enabled):
    client = FakeClient(json_response(LISTING))
    run("listing_parse", None, {"note": "x"}, client=client)
    route, req = client.calls[0]
    assert route == "messages"
    assert req["model"] == "claude-haiku-5-5"
    assert req["system"][0]["cache_control"] == {"type": "ephemeral"}
    assert req["output_config"]["effort"] == "low"
    assert req["output_config"]["format"]["type"] == "json_schema"
    assert req["output_config"]["format"]["schema"] == prompts.get("listing_parse").output_schema
    assert "thinking" not in req and "tool_choice" not in req
    assert "betas" not in req and "extra_body" not in req
    assert req["messages"] == [{"role": "user", "content": req["messages"][0]["content"]}]


def test_generation_uses_opus_with_server_side_fallback(enabled):
    client = FakeClient(json_response(STORE, model="claude-opus-5-5"))
    result = run("store_description", None, {"business_name": "Adaeze Styles"}, client=client)
    route, req = client.calls[0]
    assert route == "beta"
    assert req["model"] == "claude-opus-5-5"
    assert req["betas"] == ["server-side-fallback-2026-07-01"]
    assert req["extra_body"] == {"fallbacks": "default"}
    assert req["output_config"]["effort"] == "medium"
    assert "thinking" not in req
    assert result.data == STORE and result.model == "claude-opus-5-5"


def test_system_prompt_is_byte_stable_across_calls(enabled):
    client = FakeClient(json_response(LISTING))
    run("listing_parse", None, {"note": "first"}, client=client)
    run("listing_parse", None, {"note": "second"}, client=client)
    assert client.calls[0][1]["system"] == client.calls[1][1]["system"]


def test_prompt_registry_versions():
    assert prompts.get("listing_parse").ref == "listing_parse@v1"
    assert prompts.get("store_description@v1").kind == "generation"
    assert {"listing_parse", "store_description"} <= set(prompts.actions())
    with pytest.raises(KeyError):
        prompts.get("listing_parse@v99")


def test_unknown_action_is_a_request_error(enabled):
    with pytest.raises(AIRequestError):
        run("not_an_action", None, {}, client=FakeClient())


def test_oversized_inputs_rejected_before_call(enabled, ai_env):
    ai_env.setenv("AI_MAX_INPUT_CHARS", "50")
    client = FakeClient()
    with pytest.raises(AIRequestError):
        run("listing_parse", None, {"note": "x" * 100}, client=client)
    assert client.calls == []


# ── Structured output ───────────────────────────────────────────────────────
def test_structured_output_parsed(enabled):
    result = run("listing_parse", None, {"note": "x"}, client=FakeClient(json_response(LISTING)))
    assert result.data == LISTING
    assert result.prompt_version == "v1"


def test_caller_schema_overrides_prompt_schema(enabled):
    schema = {"type": "object", "properties": {"ok": {"type": "boolean"}},
              "required": ["ok"], "additionalProperties": False}
    client = FakeClient(json_response({"ok": True}))
    result = run("listing_parse", None, {"note": "x"}, schema=schema, client=client)
    assert result.data == {"ok": True}
    assert client.calls[0][1]["output_config"]["format"]["schema"] == schema


@pytest.mark.parametrize("bad", [
    make_response("not json"),
    json_response({"title": "x"}),                                  # missing required
    json_response({**LISTING, "price_ngn": "15k"}),                 # wrong type
    json_response({**LISTING, "extra": 1}),                         # additionalProperties
])
def test_invalid_structured_output(enabled, bad):
    with pytest.raises(AIInvalidOutput):
        run("listing_parse", None, {"note": "x"}, client=FakeClient(bad))
    assert rows()[-1].outcome == "invalid_output"


def test_max_tokens_is_reported_as_truncated(enabled):
    with pytest.raises(AIInvalidOutput):
        run("listing_parse", None, {"note": "x"},
            client=FakeClient(make_response('{"title": "An', stop_reason="max_tokens")))
    assert rows()[-1].outcome == "truncated"


# ── Refusal ─────────────────────────────────────────────────────────────────
def test_refusal_checked_before_content(enabled):
    resp = make_response("", stop_reason="refusal",
                         stop_details=SimpleNamespace(category="cyber", explanation="..."),
                         content=[])
    with pytest.raises(AIRefused) as exc:
        run("listing_parse", None, {"note": "x"}, client=FakeClient(resp))
    assert exc.value.category == "cyber"
    row = rows()[-1]
    assert row.outcome == "refused" and row.error_type == "cyber" and row.stop_reason == "refusal"
    assert row.estimated_cost_microusd > 0       # declined calls can still cost tokens


def test_server_side_fallback_metered_across_iterations(enabled):
    iterations = [
        SimpleNamespace(type="message", model="claude-opus-5-5", input_tokens=1000, output_tokens=0,
                        cache_creation_input_tokens=0, cache_read_input_tokens=0),
        SimpleNamespace(type="fallback_message", model="claude-opus-4-8", input_tokens=1000,
                        output_tokens=100, cache_creation_input_tokens=0, cache_read_input_tokens=0),
    ]
    resp = json_response(STORE, model="claude-opus-4-8", input_tokens=1000, output_tokens=100,
                         iterations=iterations)
    result = run("store_description", None, {"business_name": "x"}, client=FakeClient(resp))
    assert result.fell_back and result.served_model == "claude-opus-4-8"
    expected = (estimate_microusd("claude-opus-5-5", input_tokens=1000)
                + estimate_microusd("claude-opus-4-8", input_tokens=1000, output_tokens=100))
    row = rows()[-1]
    assert row.estimated_cost_microusd == expected
    assert row.input_tokens == 2000 and row.fell_back is True


# ── Availability ────────────────────────────────────────────────────────────
def test_missing_api_key_is_unavailable(enabled):
    with pytest.raises(AIUnavailable) as exc:
        run("listing_parse", None, {"note": "x"})      # no client injected, no key
    assert exc.value.reason == "no_api_key"
    row = rows()[-1]
    assert row.outcome == "unavailable" and row.error_type == "no_api_key"


def test_provider_exception_is_unavailable(enabled):
    client = FakeClient(error=RuntimeError("boom"))
    with pytest.raises(AIUnavailable):
        run("listing_parse", None, {"note": "x"}, client=client)
    row = rows()[-1]
    assert row.outcome == "provider_error" and row.estimated_cost_microusd == 0


def test_typed_sdk_errors_map_most_specific_first(enabled):
    anthropic = pytest.importorskip("anthropic")
    httpx = pytest.importorskip("httpx2", reason="anthropic 1.x is built on httpx2")
    req = httpx.Request("POST", "https://api.anthropic.com/v1/messages")
    err = anthropic.RateLimitError("slow down", response=httpx.Response(429, request=req), body=None)
    with pytest.raises(AIUnavailable) as exc:
        run("listing_parse", None, {"note": "x"}, client=FakeClient(error=err))
    assert exc.value.reason == "rate_limited"
    timeout = anthropic.APITimeoutError(request=req)
    with pytest.raises(AIUnavailable) as exc:
        run("listing_parse", None, {"note": "x"}, client=FakeClient(error=timeout))
    assert exc.value.reason == "timeout"


# ── Metering ────────────────────────────────────────────────────────────────
def test_metering_row_written(enabled, seller):
    resp = json_response(LISTING, input_tokens=1000, output_tokens=200, cache_read=3000)
    result = run("listing_parse", seller.id, {"note": "x"}, client=FakeClient(resp))
    [row] = rows()
    assert row.id == result.usage_id
    assert (row.action, row.prompt_version, row.model, row.seller_id) == \
        ("listing_parse", "v1", "claude-haiku-5-5", seller.id)
    assert (row.input_tokens, row.output_tokens, row.cache_read_input_tokens) == (1000, 200, 3000)
    # Haiku 5.5: $0.10 in, $0.50 out, $0.01 cache read per MTok -> micro-USD
    assert row.estimated_cost_microusd == 1000 * 0.10 + 200 * 0.50 + 3000 * 0.01 == 230
    assert row.outcome == "ok" and row.latency_ms is not None and row.created_at is not None
    assert result.estimated_cost_usd == pytest.approx(0.00023)


def test_opus_pricing_with_cache_write():
    assert estimate_microusd("claude-opus-5-5", input_tokens=1_000_000) == 4_000_000
    assert estimate_microusd("claude-opus-5-5", output_tokens=1_000_000) == 20_000_000
    assert estimate_microusd("claude-opus-5-5", cache_read_input_tokens=1_000_000) == 200_000
    assert estimate_microusd("claude-opus-5-5", cache_creation_input_tokens=1_000_000) == 5_000_000
    # Haiku long-prompt tier above 100K tokens
    assert estimate_microusd("claude-haiku-5-5", input_tokens=200_000) == 100_000


def test_metering_is_committed_independently_of_the_callers_session(enabled):
    run("listing_parse", None, {"note": "x"}, client=FakeClient(json_response(LISTING)))
    db.session.rollback()            # caller abandons its transaction
    assert len(rows()) == 1          # the usage row survives


# ── Caps ────────────────────────────────────────────────────────────────────
def test_seller_daily_action_cap(enabled, ai_env, seller, make_user):
    ai_env.setenv("AI_SELLER_DAILY_ACTION_CAP", "2")
    client = FakeClient(json_response(LISTING))
    run("listing_parse", seller.id, {"note": "1"}, client=client)
    run("listing_parse", seller.id, {"note": "2"}, client=client)
    with pytest.raises(AICapExceeded) as exc:
        run("listing_parse", seller.id, {"note": "3"}, client=client)
    assert exc.value.reason == "seller_daily_cap"
    assert len(client.calls) == 2
    assert rows()[-1].outcome == "cap_exceeded"

    other = make_user()
    run("listing_parse", other.id, {"note": "other seller"}, client=client)   # not affected


def test_per_action_cap_from_prompt_spec(enabled, seller):
    cap = prompts.get("store_description").seller_daily_cap
    client = FakeClient(json_response(STORE, model="claude-opus-5-5"))
    for _ in range(cap):
        run("store_description", seller.id, {"business_name": "x"}, client=client)
    with pytest.raises(AICapExceeded) as exc:
        run("store_description", seller.id, {"business_name": "x"}, client=client)
    assert exc.value.reason == "seller_action_cap"
    # a different action is still allowed
    run("listing_parse", seller.id, {"note": "x"}, client=FakeClient(json_response(LISTING)))


def test_failed_calls_do_not_count_toward_seller_cap(enabled, ai_env, seller):
    ai_env.setenv("AI_SELLER_DAILY_ACTION_CAP", "1")
    with pytest.raises(AIUnavailable):
        run("listing_parse", seller.id, {"note": "x"}, client=FakeClient(error=RuntimeError()))
    run("listing_parse", seller.id, {"note": "x"}, client=FakeClient(json_response(LISTING)))


def test_global_daily_cost_cap(enabled, ai_env):
    ai_env.setenv("AI_DAILY_COST_CAP_USD", "1.00")
    gateway._record(action="store_description", outcome="ok", estimated_cost_microusd=1_000_000)
    client = FakeClient(json_response(LISTING))
    with pytest.raises(AICapExceeded) as exc:
        run("listing_parse", None, {"note": "x"}, client=client)
    assert exc.value.reason == "global_cost_cap"
    assert client.calls == []


def test_yesterdays_spend_does_not_count(enabled, ai_env):
    ai_env.setenv("AI_DAILY_COST_CAP_USD", "1.00")
    yesterday = datetime.now(timezone.utc) - timedelta(days=1, hours=1)
    gateway._record(action="x", outcome="ok", estimated_cost_microusd=5_000_000, created_at=yesterday)
    assert run("listing_parse", None, {"note": "x"}, client=FakeClient(json_response(LISTING))).data


# ── Evals ───────────────────────────────────────────────────────────────────
def test_eval_case_files_are_valid():
    for path in CASES_DIR.glob("*.json"):
        data = load_cases(path)
        prompts.get(data["action"])          # every eval set targets a registered prompt


def test_eval_runner_offline_with_fake_client(enabled):
    def answer(req):
        note = req["messages"][0]["content"]
        price = 15000 if "15k" in note else 4500 if "4500" in note else None
        qty = 3 if "3 left" in note else None
        title = ("Ankara maxi dress" if "Ankara" in note else "Leather sandals" if "sandals" in note
                 else "iPhone 13" if "iPhone" in note else "Shea butter tub")
        return json_response({"title": title, "price_ngn": price, "quantity": qty, "attributes": []})

    report = run_eval(CASES_DIR / "listing_parse.v1.json", client=FakeClient(answer))
    assert report.total == 4 and report.passed == 4, report.summary()


def test_eval_runner_reports_failures_and_errors(enabled):
    eval_set = {"action": "listing_parse", "cases": [
        {"id": "wrong", "inputs": {"note": "a"}, "expect": {"equals": {"price_ngn": 1}}},
    ]}
    report = run_eval(eval_set, client=FakeClient(json_response(LISTING)))
    assert report.passed == 0 and "price_ngn" in report.summary()

    report = run_eval(eval_set, client=FakeClient(error=RuntimeError()))
    assert report.results[0].error.startswith("AIUnavailable")


def test_eval_check_banned_phrases():
    assert check({"headline": "Verified seller"}, {"must_not_contain": ["verified"]})
    assert not check({"headline": "Fresh bread"}, {"must_not_contain": ["verified"],
                                                   "max_length": {"headline": 60}})
