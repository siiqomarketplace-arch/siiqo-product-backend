"""
Speech-to-text for bot voice notes (Phase T, T-4). Behind VOICE_NOTES_ENABLED (default off).

Provider interface: ``provider.transcribe(audio, mime_type, filename) -> Transcript``.
The only implementation is ElevenLabs, built from these documented fields of
``POST https://api.elevenlabs.io/v1/speech-to-text`` (checked against the API
reference page on 31 Oct 2026; no live call was made):

  auth header   xi-api-key
  body          multipart/form-data
    model_id      required   (reference example: "scribe_v2"; env ELEVENLABS_STT_MODEL)
    file          the audio  (minimum 100 ms)
    language_code optional   ISO-639-1/3; omitted -> detected (env ELEVENLABS_STT_LANGUAGE)
  response      text, language_code, language_probability, words

Audio stays in memory: it is never written to disk or the database, and the
transcript is returned to the bot without being logged or stored.
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass

import requests

log = logging.getLogger(__name__)

ELEVENLABS_STT_URL = "https://api.elevenlabs.io/v1/speech-to-text"
DEFAULT_MODEL = "scribe_v2"
TIMEOUT_SECONDS = 25


class STTError(Exception):
    def __init__(self, message: str, reason: str = "error"):
        super().__init__(message)
        self.reason = reason


@dataclass
class Transcript:
    text: str
    language_code: str | None = None
    language_probability: float | None = None


class ElevenLabsSTT:
    def __init__(self, api_key: str, model_id: str | None = None, language_code: str | None = None,
                 session=None, url: str = ELEVENLABS_STT_URL):
        self._key = api_key
        self.model_id = model_id or DEFAULT_MODEL
        self.language_code = language_code or None
        self._http = session or requests
        self.url = url

    def transcribe(self, audio: bytes, mime_type: str, filename: str = "voice.ogg") -> Transcript:
        data = {"model_id": self.model_id}
        if self.language_code:
            data["language_code"] = self.language_code
        try:
            resp = self._http.post(
                self.url,
                headers={"xi-api-key": self._key},
                data=data,
                files={"file": (filename, audio, mime_type)},
                timeout=TIMEOUT_SECONDS,
            )
        except requests.RequestException as exc:
            raise STTError("speech-to-text provider unreachable", "connection") from exc
        status = getattr(resp, "status_code", 0)
        if status == 429:
            raise STTError("speech-to-text provider rate limit", "rate_limited")
        if status in (401, 403):
            raise STTError("speech-to-text provider rejected the key", "auth")
        if status >= 400 or status == 0:
            raise STTError(f"speech-to-text provider error {status}", f"status_{status}")
        try:
            body = resp.json()
        except ValueError as exc:
            raise STTError("speech-to-text provider returned invalid JSON", "invalid_output") from exc
        text = body.get("text") if isinstance(body, dict) else None
        if not isinstance(text, str):
            raise STTError("speech-to-text response has no text", "invalid_output")
        prob = body.get("language_probability")
        return Transcript(
            text=text.strip(),
            language_code=body.get("language_code") if isinstance(body.get("language_code"), str) else None,
            language_probability=float(prob) if isinstance(prob, (int, float)) else None,
        )


def voice_enabled() -> bool:
    return (os.environ.get("VOICE_NOTES_ENABLED") or "").strip().lower() in ("1", "true", "yes", "on")


def get_provider():
    """The configured provider, or None when ELEVENLABS_API_KEY is unset."""
    key = (os.environ.get("ELEVENLABS_API_KEY") or "").strip()
    if not key:
        return None
    return ElevenLabsSTT(
        key,
        model_id=(os.environ.get("ELEVENLABS_STT_MODEL") or "").strip() or None,
        language_code=(os.environ.get("ELEVENLABS_STT_LANGUAGE") or "").strip() or None,
    )
