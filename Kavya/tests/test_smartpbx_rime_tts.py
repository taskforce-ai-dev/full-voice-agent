"""Rime Arcana canary coverage for Direct SmartPBX Sinhala only.

The canary must be a reversible Sinhala-only provider selection.  It never
changes Twilio or English routing, and every Rime failure gets exactly one
existing-Gemini attempt through the established media path.
"""

from __future__ import annotations

import asyncio
import logging

import pytest


class FakeTransport:
    def __init__(self) -> None:
        self.audio: list[bytes] = []
        self.marks: list[str] = []

    async def send_audio(self, audio: bytes) -> None:
        self.audio.append(audio)

    async def clear_audio(self) -> int:
        return 0

    async def send_mark(self, name: str) -> None:
        self.marks.append(name)


def make_direct_sinhala(server):
    transport = FakeTransport()
    pipeline = server.MediaStreamSession(
        websocket=None, lang="si", media_transport=transport, llm_provider="gemini",
    )
    pipeline._smartpbx_transfer_context = object()
    return pipeline, transport


def test_rime_provider_selector_defaults_to_gemini_and_rejects_unknown_values():
    import server

    assert server._resolve_smartpbx_sinhala_tts_provider(None) == "gemini"
    assert server._resolve_smartpbx_sinhala_tts_provider("") == "gemini"
    assert server._resolve_smartpbx_sinhala_tts_provider("RIME") == "rime"
    assert server._resolve_smartpbx_sinhala_tts_provider("other") == "gemini"


def test_rime_arcana_request_shape_is_exact_and_has_no_operator_supplied_fields():
    import server

    assert server._rime_arcana_request_payload("සිංහල") == {
        "text": "සිංහල",
        "modelId": "arcana",
        "speaker": "chandani",
        "lang": "si",
        "max_tokens": 1200,
        "repetition_penalty": 1.6,
        "samplingRate": 24000,
        "speedAlpha": 1,
        "temperature": 0.5,
        "top_p": 1,
    }


@pytest.mark.asyncio
async def test_rime_request_uses_supplied_endpoint_headers_and_shared_timeout(monkeypatch):
    import server

    captured: dict[str, object] = {}

    class Response:
        status_code = 200

        def raise_for_status(self):
            return None

        async def aiter_bytes(self):
            yield b"mp3"

    class Stream:
        async def __aenter__(self):
            return Response()

        async def __aexit__(self, *_args):
            return False

    class Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

        def stream(self, method, url, **kwargs):
            captured.update(method=method, url=url, **kwargs)
            return Stream()

    monkeypatch.setattr(server, "RIME_API_KEY", "test-rime-key")
    monkeypatch.setattr(server.httpx, "AsyncClient", Client)

    assert await server._request_rime_arcana_mp3("සිංහල") == b"mp3"
    assert captured == {
        "method": "POST",
        "url": "https://users.rime.ai/v1/rime-tts",
        "json": server._rime_arcana_request_payload("සිංහල"),
        "headers": {
            "Accept": "audio/mp3",
            "Authorization": "Bearer test-rime-key",
            "Content-Type": "application/json",
        },
        "timeout": server.SMARTPBX_SINHALA_GEMINI_TTS_TIMEOUT_SECONDS,
    }


@pytest.mark.asyncio
async def test_rime_mp3_decoder_uses_fixed_ffmpeg_mulaw_contract(monkeypatch):
    import server

    captured: dict[str, object] = {}

    class Process:
        returncode = 0

        async def communicate(self, audio):
            captured["audio"] = audio
            return b"\x01\x02", b""

    async def create_subprocess_exec(*args, **kwargs):
        captured["args"] = args
        captured["kwargs"] = kwargs
        return Process()

    monkeypatch.setattr(
        server.asyncio, "create_subprocess_exec", create_subprocess_exec,
    )

    assert await server._decode_rime_arcana_mp3_to_mulaw(b"mp3-bytes") == b"\x01\x02"
    assert captured["args"] == (
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-i", "pipe:0",
        "-ac", "1", "-ar", "8000", "-f", "mulaw", "pipe:1",
    )
    assert captured["kwargs"] == {
        "stdin": asyncio.subprocess.PIPE,
        "stdout": asyncio.subprocess.PIPE,
        "stderr": asyncio.subprocess.DEVNULL,
    }
    assert captured["audio"] == b"mp3-bytes"


@pytest.mark.asyncio
async def test_rime_route_is_limited_to_direct_smartpbx_sinhala(monkeypatch):
    import server

    monkeypatch.setattr(server, "SMARTPBX_SINHALA_TTS_PROVIDER", "rime")
    direct_sinhala, _ = make_direct_sinhala(server)
    direct_english = server.MediaStreamSession(
        websocket=None, lang="en", media_transport=FakeTransport(), llm_provider="claude",
    )
    direct_english._smartpbx_transfer_context = object()
    twilio_sinhala = server.MediaStreamSession(websocket=None, lang="si")
    routes: list[str] = []

    async def rime(*_args, **_kwargs):
        routes.append("rime")

    async def gemini(*_args, **_kwargs):
        routes.append("gemini")

    async def elevenlabs(*_args, **_kwargs):
        routes.append("elevenlabs")

    async def openai(*_args, **_kwargs):
        routes.append("openai")

    for pipeline in (direct_sinhala, direct_english, twilio_sinhala):
        monkeypatch.setattr(pipeline, "_tts_rime_sinhala", rime)
        monkeypatch.setattr(pipeline, "_tts_gemini_sinhala", gemini)
        monkeypatch.setattr(pipeline, "_tts_elevenlabs", elevenlabs)
        monkeypatch.setattr(pipeline, "_tts_openai", openai)

    await direct_sinhala._speak("සිංහල")
    await direct_english._speak("English")
    await twilio_sinhala._speak("සිංහල")

    assert routes == ["rime", "elevenlabs", "openai"]


@pytest.mark.asyncio
async def test_rime_decoded_mulaw_uses_existing_transport_framing_and_mark(monkeypatch):
    import server

    pipeline, transport = make_direct_sinhala(server)
    monkeypatch.setattr(server, "RIME_API_KEY", "test-rime-key")

    async def request(_text):
        return b"mp3"

    async def decode(_audio):
        return b"\x00" * 641

    monkeypatch.setattr(server, "_request_rime_arcana_mp3", request)
    monkeypatch.setattr(server, "_decode_rime_arcana_mp3_to_mulaw", decode)

    await pipeline._tts_rime_sinhala("සිංහල", sentence="සිංහල")

    assert [len(frame) for frame in transport.audio] == [640, 640]
    assert transport.marks == ["tts_done"]


@pytest.mark.asyncio
async def test_rime_selector_keeps_cached_fixed_phrase_on_existing_gemini_playback_path(
    monkeypatch,
):
    import server

    pipeline, transport = make_direct_sinhala(server)
    text = server.SMARTPBX_SINHALA_INITIAL_FILLER_TEXT
    monkeypatch.setattr(server, "SMARTPBX_SINHALA_TTS_PROVIDER", "rime")
    monkeypatch.setattr(server, "GEMINI_API_KEY", "test-gemini-key")
    monkeypatch.setattr(server, "_SMARTPBX_SINHALA_PHRASE_AUDIO", {})
    server._store_cached_smartpbx_sinhala_phrase_audio(text, b"\x00" * 640)

    async def rime_must_not_run(*_args, **_kwargs):
        raise AssertionError("cached fixed phrase must not request Rime")

    monkeypatch.setattr(pipeline, "_tts_rime_sinhala", rime_must_not_run)

    await pipeline._speak(text, sentence=text)

    assert transport.audio == [b"\x00" * 640]
    assert transport.marks == ["tts_done"]


@pytest.mark.asyncio
async def test_rime_stale_generation_never_emits_decoded_audio(monkeypatch):
    import server

    pipeline, transport = make_direct_sinhala(server)
    monkeypatch.setattr(server, "RIME_API_KEY", "test-rime-key")

    async def request(_text):
        return b"mp3"

    async def decode(_audio):
        pipeline._speak_generation += 1
        return b"\x00" * 640

    monkeypatch.setattr(server, "_request_rime_arcana_mp3", request)
    monkeypatch.setattr(server, "_decode_rime_arcana_mp3_to_mulaw", decode)

    await pipeline._tts_rime_sinhala("සිංහල")

    assert transport.audio == []
    assert transport.marks == []


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", ["timeout", "empty_audio", "decode_failure"])
async def test_silent_rime_failures_fall_back_to_gemini_once_without_logging_secret(
    monkeypatch, caplog, outcome,
):
    import server

    pipeline, _ = make_direct_sinhala(server)
    secret = "test-rime-secret-must-not-appear"
    monkeypatch.setattr(server, "RIME_API_KEY", secret)
    calls: list[str] = []

    async def request(_text):
        raise server._RimeArcanaTTSFailure(outcome)

    async def gemini(text, **_kwargs):
        calls.append(text)

    monkeypatch.setattr(server, "_request_rime_arcana_mp3", request)
    monkeypatch.setattr(pipeline, "_tts_gemini_sinhala", gemini)
    caplog.set_level(logging.INFO)

    await pipeline._tts_rime_sinhala("සිංහල", sentence="සිංහල")

    assert calls == ["සිංහල"]
    assert secret not in caplog.text


@pytest.mark.asyncio
async def test_rime_never_falls_back_after_an_audio_frame_was_accepted(monkeypatch):
    import server

    pipeline, _ = make_direct_sinhala(server)
    monkeypatch.setattr(server, "RIME_API_KEY", "test-rime-key")
    gemini_calls: list[str] = []
    sends = 0

    async def request(_text):
        return b"mp3"

    async def decode(_audio):
        return b"\x00" * 1280

    async def send(_frame):
        nonlocal sends
        sends += 1
        if sends == 2:
            raise RuntimeError("transport broke after the first frame")
        return True

    async def gemini(text, **_kwargs):
        gemini_calls.append(text)

    monkeypatch.setattr(server, "_request_rime_arcana_mp3", request)
    monkeypatch.setattr(server, "_decode_rime_arcana_mp3_to_mulaw", decode)
    monkeypatch.setattr(pipeline, "_send_media_audio", send)
    monkeypatch.setattr(pipeline, "_tts_gemini_sinhala", gemini)

    await pipeline._tts_rime_sinhala("සිංහල")

    assert sends == 2
    assert gemini_calls == []
