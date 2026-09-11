"""Website demo language routing for /voice/demo-incoming.

All four demo languages must be honoured:
  en, ru -> Twilio ConversationRelay (Twilio owns STT + TTS)
  ar, si -> Twilio Media Streams (we own STT/TTS; CR has no ar/si locale)
An unknown language falls back to English ConversationRelay.

The TestClient is used WITHOUT its context manager on purpose, so the app's
lifespan (KB prewarm, client init) does not run — this route needs none of it.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

HORIZON = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HORIZON))

import server  # noqa: E402

client = TestClient(server.app)


def _twiml(lang: str) -> str:
    # No `agent` param -> route renders locally rather than <Redirect>ing.
    resp = client.post("/voice/demo-incoming", data={"lang": lang})
    assert resp.status_code == 200
    return resp.text


@pytest.mark.parametrize("lang", ["ar", "si"])
def test_media_stream_languages(lang):
    body = _twiml(lang)
    assert f"/ws/media-stream/{lang}" in body
    assert "<Connect>" in body


@pytest.mark.parametrize("lang,locale", [("en", "en-US"), ("ru", "ru-RU")])
def test_conversationrelay_languages(lang, locale):
    body = _twiml(lang)
    assert "ConversationRelay" in body
    assert locale in body
    # must NOT have been routed to Media Streams
    assert "/ws/media-stream/" not in body


def test_unknown_language_falls_back_to_english_cr():
    body = _twiml("xx")
    assert "ConversationRelay" in body
    assert "en-US" in body


def test_media_stream_ws_guard_accepts_all_offered_media_langs():
    """ar and si must be accepted by the media-stream websocket guard; en/ru
    ride ConversationRelay and never reach it."""
    # The guard lives in ws_media_stream: `if lang not in (...) : lang = "si"`.
    # Assert the source tuple covers ar and si (defensive structural check).
    import inspect
    src = inspect.getsource(server.ws_media_stream)
    assert '"ar"' in src and '"si"' in src
