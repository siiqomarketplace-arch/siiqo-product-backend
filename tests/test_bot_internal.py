"""
Phase T / W internal bot API: signing and replay, durable sessions (TTL, size
limit), the free-text intent router (AI off, mocked AI, low confidence, rate
limit, cache) and voice-note transcription (flag off, mocked provider, limits).
No network: the AI client and the speech-to-text provider are fakes.
"""
import base64
import json
import time
import uuid
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from app.extensions import db
from app.models.ai import AIUsage
from app.models.bot import BotIntentCache, BotSession
from app.services import bot_auth, bot_intent, speech_to_text
from app.services.ai import gateway

SECRET = "x" * 40
BASE = "/api/internal/bot"


@pytest.fixture(autouse=True)
def bot_env(monkeypatch):
    for key in ("AI_ENABLED", "AI_ENABLED_ACTIONS", "ANTHROPIC_API_KEY", "VOICE_NOTES_ENABLED",
                "ELEVENLABS_API_KEY", "BOT_SESSION_MAX_BYTES", "BOT_SESSION_TTL_SECONDS",
                "BOT_SESSION_MAX_TTL_SECONDS", "BOT_INTENT_RATE_PER_MINUTE", "BOT_INTENT_RATE_PER_DAY",
                "BOT_INTENT_MIN_CONFIDENCE", "VOICE_MAX_SECONDS", "VOICE_MAX_BYTES",
                "VOICE_RATE_PER_DAY", "VOICE_RATE_PER_MINUTE", "BOT_INTERNAL_MAX_SKEW_SECONDS"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("BOT_INTERNAL_SECRET", SECRET)
    return monkeypatch


def call(client, method, path, body=None, *, secret=SECRET, ts=None, nonce=None, tamper=None):
    raw = b"" if body is None else json.dumps(body).encode()
    ts = int(time.time()) if ts is None else ts
    nonce = nonce or uuid.uuid4().hex
    headers = {"Content-Type": "application/json"}
    if secret is not None:
        headers[bot_auth.SIGNATURE_HEADER] = bot_auth.sign(secret, ts, nonce, method, path, raw)
    if tamper:
        raw = tamper(raw)
    return client.open(path, method=method, data=raw if raw else None, headers=headers)


# ── Signing ─────────────────────────────────────────────────────────────────
def test_fails_closed_without_secret(client, bot_env):
    bot_env.delenv("BOT_INTERNAL_SECRET")
    res = call(client, "GET", f"{BASE}/sessions/telegram/42")
    assert res.status_code == 503
    assert res.get_json()["code"] == "BOT_INTERNAL_DISABLED"


def test_short_secret_counts_as_unset(client, bot_env):
    bot_env.setenv("BOT_INTERNAL_SECRET", "short")
    res = call(client, "GET", f"{BASE}/sessions/telegram/42", secret="short")
    assert res.status_code == 503


def test_unsigned_and_wrong_secret_rejected(client):
    assert call(client, "GET", f"{BASE}/sessions/telegram/42", secret=None).status_code == 401
    assert call(client, "GET", f"{BASE}/sessions/telegram/42", secret="y" * 40).status_code == 401


def test_stale_timestamp_rejected(client):
    old = int(time.time()) - 301
    assert call(client, "GET", f"{BASE}/sessions/telegram/42", ts=old).status_code == 401
    future = int(time.time()) + 301
    assert call(client, "GET", f"{BASE}/sessions/telegram/42", ts=future).status_code == 401


def test_body_tampering_rejected(client):
    res = call(client, "PUT", f"{BASE}/sessions/telegram/42", {"payload": "abc"},
               tamper=lambda raw: raw.replace(b"abc", b"xyz"))
    assert res.status_code == 401


def test_signature_bound_to_path_and_method(client):
    raw = json.dumps({"payload": "abc"}).encode()
    ts, nonce = int(time.time()), uuid.uuid4().hex
    header = bot_auth.sign(SECRET, ts, nonce, "PUT", f"{BASE}/sessions/telegram/42", raw)
    other_user = client.put(f"{BASE}/sessions/telegram/43", data=raw,
                            headers={bot_auth.SIGNATURE_HEADER: header, "Content-Type": "application/json"})
    assert other_user.status_code == 401
    as_delete = client.delete(f"{BASE}/sessions/telegram/42",
                              headers={bot_auth.SIGNATURE_HEADER: header})
    assert as_delete.status_code == 401


def test_replayed_write_rejected(client):
    nonce = uuid.uuid4().hex
    ts = int(time.time())
    first = call(client, "PUT", f"{BASE}/sessions/telegram/42", {"payload": "abc"}, ts=ts, nonce=nonce)
    assert first.status_code == 200
    replay = call(client, "PUT", f"{BASE}/sessions/telegram/42", {"payload": "abc"}, ts=ts, nonce=nonce)
    assert replay.status_code == 409


def test_bad_nonce_format_rejected(client):
    assert call(client, "GET", f"{BASE}/sessions/telegram/42", nonce="short").status_code == 401


# ── Sessions ────────────────────────────────────────────────────────────────
def test_session_round_trip_and_delete(client):
    path = f"{BASE}/sessions/telegram/42"
    assert call(client, "GET", path).status_code == 404
    assert call(client, "PUT", path, {"payload": "v1.encrypted"}).status_code == 200
    got = call(client, "GET", path)
    assert got.status_code == 200 and got.get_json()["payload"] == "v1.encrypted"
    assert call(client, "PUT", path, {"payload": "v1.second"}).status_code == 200
    assert call(client, "GET", path).get_json()["payload"] == "v1.second"
    assert BotSession.query.count() == 1
    assert call(client, "DELETE", path).status_code == 200
    assert call(client, "GET", path).status_code == 404


def test_sessions_are_per_platform(client):
    call(client, "PUT", f"{BASE}/sessions/telegram/42", {"payload": "tg"})
    call(client, "PUT", f"{BASE}/sessions/whatsapp/42", {"payload": "wa"})
    assert call(client, "GET", f"{BASE}/sessions/telegram/42").get_json()["payload"] == "tg"
    assert call(client, "GET", f"{BASE}/sessions/whatsapp/42").get_json()["payload"] == "wa"


@pytest.mark.parametrize("platform,user", [("sms", "42"), ("telegram", "bad id!"), ("telegram", "a" * 65)])
def test_session_identity_validated(client, platform, user):
    assert call(client, "GET", f"{BASE}/sessions/{platform}/{user}").status_code in (400, 404)


def test_session_ttl_expiry(client):
    path = f"{BASE}/sessions/telegram/42"
    call(client, "PUT", path, {"payload": "abc", "ttl_seconds": 120})
    row = BotSession.query.one()
    ttl = (row.expires_at.replace(tzinfo=timezone.utc) - datetime.now(timezone.utc)).total_seconds()
    assert 100 < ttl <= 120
    row.expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
    db.session.commit()
    assert call(client, "GET", path).status_code == 404
    assert BotSession.query.count() == 0


def test_session_ttl_clamped(client, bot_env):
    bot_env.setenv("BOT_SESSION_MAX_TTL_SECONDS", "3600")
    call(client, "PUT", f"{BASE}/sessions/telegram/42", {"payload": "abc", "ttl_seconds": 10 ** 9})
    row = BotSession.query.one()
    ttl = (row.expires_at.replace(tzinfo=timezone.utc) - datetime.now(timezone.utc)).total_seconds()
    assert ttl <= 3600
    call(client, "PUT", f"{BASE}/sessions/telegram/42", {"payload": "abc", "ttl_seconds": 1})
    db.session.expire_all()
    row = BotSession.query.one()
    ttl = (row.expires_at.replace(tzinfo=timezone.utc) - datetime.now(timezone.utc)).total_seconds()
    assert ttl >= 55


def test_expired_rows_pruned_on_write(client):
    call(client, "PUT", f"{BASE}/sessions/telegram/1", {"payload": "old"})
    BotSession.query.update({BotSession.expires_at: datetime.now(timezone.utc) - timedelta(days=1)})
    db.session.commit()
    call(client, "PUT", f"{BASE}/sessions/telegram/2", {"payload": "new"})
    assert [r.user_ref for r in BotSession.query.all()] == ["2"]


def test_session_size_limit(client, bot_env):
    bot_env.setenv("BOT_SESSION_MAX_BYTES", "100")
    path = f"{BASE}/sessions/telegram/42"
    assert call(client, "PUT", path, {"payload": "a" * 100}).status_code == 200
    res = call(client, "PUT", path, {"payload": "a" * 101})
    assert res.status_code == 413 and res.get_json()["code"] == "TOO_LARGE"


@pytest.mark.parametrize("body", [{}, {"payload": ""}, {"payload": 5}, {"payload": ["x"]}])
def test_session_payload_validated(client, body):
    assert call(client, "PUT", f"{BASE}/sessions/telegram/42", body).status_code == 400


# ── Intent router ───────────────────────────────────────────────────────────
def make_response(obj):
    usage = SimpleNamespace(input_tokens=300, output_tokens=40, cache_creation_input_tokens=0,
                            cache_read_input_tokens=0, iterations=None)
    return SimpleNamespace(id="msg_test", model="claude-haiku-5-5", stop_reason="end_turn",
                           stop_details=None, usage=usage,
                           content=[SimpleNamespace(type="text", text=json.dumps(obj))])


class FakeClient:
    def __init__(self, obj=None, error=None):
        self.obj, self.error, self.calls = obj, error, []
        self.messages = SimpleNamespace(create=self._create)

    def _create(self, **kwargs):
        self.calls.append(kwargs)
        if self.error:
            raise self.error
        return make_response(self.obj)


@pytest.fixture
def fake_ai(bot_env):
    holder = {}

    def install(obj=None, error=None):
        holder["client"] = FakeClient(obj, error)
        bot_env.setenv("AI_ENABLED", "1")
        bot_env.setattr(gateway, "_default_client", lambda config: holder["client"])
        return holder["client"]
    return install


def intent(client, text, user="42", platform="telegram"):
    return call(client, "POST", f"{BASE}/intent", {"platform": platform, "user_id": user, "text": text})


def test_intent_ai_off_falls_back_to_menu(client):
    res = intent(client, "abeg I wan know if this person go deliver")
    body = res.get_json()
    assert res.status_code == 200
    assert body["intent"] is None and body["fallback"] == "menu" and body["reason"] == "ai_disabled"


def test_intent_phrases_work_without_ai(client):
    assert intent(client, "My orders!").get_json()["intent"] == "my_orders"
    body = intent(client, "looking for ankara gown").get_json()
    assert body["intent"] == "search" and body["query"] == "ankara gown" and body["source"] == "phrase"


def test_intent_short_or_numeric_text_skips_ai(client, fake_ai):
    fake = fake_ai({"intent": "help", "confidence": 1, "alternatives": [], "query": None})
    assert intent(client, "7").get_json()["fallback"] == "menu"
    assert intent(client, "12345").get_json()["fallback"] == "menu"
    assert fake.calls == []


def test_intent_mocked_ai_confident(client, fake_ai):
    fake = fake_ai({"intent": "search", "confidence": 0.93, "alternatives": [], "query": "phone in ikeja"})
    body = intent(client, "where I fit buy phone for Ikeja").get_json()
    assert body == {**body, "intent": "search", "query": "phone in ikeja", "source": "ai",
                    "needs_confirmation": False, "fallback": None}
    assert len(fake.calls) == 1
    req = fake.calls[0]
    assert req["model"] == "claude-haiku-5-5"
    assert req["output_config"]["format"]["schema"]["properties"]["intent"]["enum"] == list(bot_intent.INTENTS)
    usage = AIUsage.query.one()
    assert usage.action == "bot_intent" and usage.seller_id is None


def test_intent_personal_data_stripped_before_ai(client, fake_ai):
    fake = fake_ai({"intent": "check_seller", "confidence": 0.9, "alternatives": [], "query": None})
    intent(client, "is 08031234567 a real seller")
    sent = fake.calls[0]["messages"][0]["content"]
    assert "08031234567" not in sent and "[phone]" in sent


def test_intent_low_confidence_returns_choices(client, fake_ai):
    fake_ai({"intent": "create_pay_link", "confidence": 0.4,
             "alternatives": ["my_orders", "help", "search"], "query": None})
    body = intent(client, "money matter for customer").get_json()
    assert body["needs_confirmation"] is True
    assert body["choices"] == ["create_pay_link", "my_orders"]
    assert body["fallback"] is None


def test_intent_ai_error_falls_back(client, fake_ai):
    fake_ai(error=RuntimeError("boom"))
    body = intent(client, "something the phrases do not know").get_json()
    assert body["fallback"] == "menu" and body["reason"] == "ai_unavailable"


def test_intent_invalid_ai_output_falls_back(client, fake_ai):
    fake_ai({"intent": "transfer_money", "confidence": 0.99, "alternatives": [], "query": None})
    body = intent(client, "send 50k to my brother").get_json()
    assert body["fallback"] == "menu" and body["intent"] is None


def test_intent_common_phrasing_cached(client, fake_ai):
    fake = fake_ai({"intent": "my_orders", "confidence": 0.95, "alternatives": [], "query": None})
    assert intent(client, "my package never reach o").get_json()["source"] == "ai"
    second = intent(client, "My package never reach o!!").get_json()
    assert second["source"] == "cache" and second["intent"] == "my_orders"
    assert len(fake.calls) == 1
    row = BotIntentCache.query.one()
    assert row.hits == 1 and len(row.text_hash) == 64      # hash only, no text stored


def test_intent_with_query_not_cached(client, fake_ai):
    fake = fake_ai({"intent": "search", "confidence": 0.95, "alternatives": [], "query": "rice"})
    intent(client, "I dey find bag of rice")
    intent(client, "I dey find bag of rice")
    assert len(fake.calls) == 2 and BotIntentCache.query.count() == 0


def test_intent_cache_ignored_when_ai_switched_off(client, fake_ai, bot_env):
    fake_ai({"intent": "my_orders", "confidence": 0.95, "alternatives": [], "query": None})
    intent(client, "my package never reach o")
    bot_env.setenv("AI_ENABLED", "0")
    assert intent(client, "my package never reach o").get_json()["reason"] == "ai_disabled"


def test_intent_rate_limit_per_user(client, bot_env):
    bot_env.setenv("BOT_INTENT_RATE_PER_MINUTE", "3")
    for _ in range(3):
        assert intent(client, "help").status_code == 200
    limited = intent(client, "help")
    assert limited.status_code == 429
    assert limited.get_json()["fallback"] == "menu"
    assert intent(client, "help", user="43").status_code == 200          # other users unaffected
    assert intent(client, "help", platform="whatsapp").status_code == 200


def test_intent_requires_text(client):
    assert call(client, "POST", f"{BASE}/intent", {"platform": "telegram", "user_id": "1"}).status_code == 400


def test_bot_intent_prompt_registered():
    from app.services.ai import prompts
    spec = prompts.get("bot_intent")
    assert spec.ref == "bot_intent@v1" and spec.kind == "parsing"
    assert set(spec.output_schema["properties"]["intent"]["enum"]) == set(bot_intent.INTENTS)


# ── Voice notes ─────────────────────────────────────────────────────────────
class FakeSTT:
    def __init__(self, text="I want to check a seller", error=None):
        self.text, self.error, self.calls = text, error, []

    def transcribe(self, audio, mime_type, filename="voice.ogg"):
        self.calls.append((len(audio), mime_type, filename))
        if self.error:
            raise self.error
        return speech_to_text.Transcript(text=self.text, language_code="eng")


def voice_body(audio=b"OggS" + b"\0" * 200, duration=5, mime="audio/ogg"):
    return {"platform": "telegram", "user_id": "42", "mime_type": mime,
            "duration_seconds": duration, "audio_base64": base64.b64encode(audio).decode()}


@pytest.fixture
def voice_on(bot_env):
    bot_env.setenv("VOICE_NOTES_ENABLED", "true")
    fake = FakeSTT()
    bot_env.setattr(speech_to_text, "get_provider", lambda: fake)
    return fake


def test_transcribe_flag_off(client, bot_env):
    stt = FakeSTT()
    bot_env.setattr(speech_to_text, "get_provider", lambda: stt)
    res = call(client, "POST", f"{BASE}/transcribe", voice_body())
    assert res.status_code == 503 and res.get_json()["code"] == "VOICE_DISABLED"
    assert stt.calls == []


def test_transcribe_mocked_provider(client, voice_on):
    res = call(client, "POST", f"{BASE}/transcribe", voice_body())
    assert res.status_code == 200
    assert res.get_json() == {"text": "I want to check a seller", "language_code": "eng"}
    assert voice_on.calls == [(204, "audio/ogg", "voice.ogg")]


def test_transcribe_without_key_unavailable(client, bot_env):
    bot_env.setenv("VOICE_NOTES_ENABLED", "1")
    res = call(client, "POST", f"{BASE}/transcribe", voice_body())
    assert res.status_code == 503 and res.get_json()["code"] == "VOICE_UNAVAILABLE"


def test_transcribe_duration_limit(client, voice_on, bot_env):
    bot_env.setenv("VOICE_MAX_SECONDS", "30")
    res = call(client, "POST", f"{BASE}/transcribe", voice_body(duration=31))
    assert res.status_code == 413 and voice_on.calls == []


def test_transcribe_size_limit(client, voice_on, bot_env):
    bot_env.setenv("VOICE_MAX_BYTES", "1000")
    res = call(client, "POST", f"{BASE}/transcribe", voice_body(audio=b"a" * 1001))
    assert res.status_code == 413 and voice_on.calls == []
    assert call(client, "POST", f"{BASE}/transcribe", voice_body(audio=b"a" * 1000)).status_code == 200


@pytest.mark.parametrize("mime", ["image/png", "video/mp4", ""])
def test_transcribe_mime_checked(client, voice_on, mime):
    assert call(client, "POST", f"{BASE}/transcribe", voice_body(mime=mime)).status_code == 415


def test_transcribe_invalid_base64(client, voice_on):
    body = voice_body()
    body["audio_base64"] = "not base64 !!"
    assert call(client, "POST", f"{BASE}/transcribe", body).status_code == 400


def test_transcribe_provider_error(client, voice_on):
    voice_on.error = speech_to_text.STTError("down", "connection")
    res = call(client, "POST", f"{BASE}/transcribe", voice_body())
    assert res.status_code == 502


def test_transcribe_rate_limited(client, voice_on, bot_env):
    bot_env.setenv("VOICE_RATE_PER_MINUTE", "2")
    for _ in range(2):
        assert call(client, "POST", f"{BASE}/transcribe", voice_body()).status_code == 200
    assert call(client, "POST", f"{BASE}/transcribe", voice_body()).status_code == 429


def test_elevenlabs_adapter_uses_documented_fields():
    sent = {}

    class Session:
        def post(self, url, headers=None, data=None, files=None, timeout=None):
            sent.update(url=url, headers=headers, data=data, files=files, timeout=timeout)
            return SimpleNamespace(status_code=200, json=lambda: {
                "text": " hello ", "language_code": "eng", "language_probability": 0.9, "words": []})

    stt = speech_to_text.ElevenLabsSTT("test-key", session=Session())
    out = stt.transcribe(b"audio", "audio/ogg", "voice.ogg")
    assert out.text == "hello" and out.language_code == "eng"
    assert sent["url"] == "https://api.elevenlabs.io/v1/speech-to-text"
    assert sent["headers"] == {"xi-api-key": "test-key"}
    assert sent["data"] == {"model_id": "scribe_v2"}
    assert sent["files"]["file"] == ("voice.ogg", b"audio", "audio/ogg")


@pytest.mark.parametrize("status,reason", [(401, "auth"), (429, "rate_limited"), (500, "status_500")])
def test_elevenlabs_adapter_errors(status, reason):
    class Session:
        def post(self, *a, **k):
            return SimpleNamespace(status_code=status, json=lambda: {})
    with pytest.raises(speech_to_text.STTError) as err:
        speech_to_text.ElevenLabsSTT("k", session=Session()).transcribe(b"a", "audio/ogg")
    assert err.value.reason == reason


def test_provider_none_without_key(bot_env):
    assert speech_to_text.get_provider() is None
