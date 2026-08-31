"""Gemini-only Sinhala TTS contract coverage for Dialog SmartPBX."""

import base64
import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest


class FakeTransport:
    def __init__(self):
        self.audio: list[bytes] = []
        self.marks: list[str] = []

    async def send_audio(self, audio: bytes) -> None:
        self.audio.append(audio)

    async def clear_audio(self) -> int:
        return 0

    async def send_mark(self, name: str) -> None:
        self.marks.append(name)


class FakeAsyncStream:
    def __init__(self, events):
        self._events = iter(events)

    def __aiter__(self):
        return self

    async def __anext__(self):
        try:
            return next(self._events)
        except StopIteration:
            raise StopAsyncIteration from None


class FakeInteractions:
    def __init__(self, stream=None, error=None):
        self.stream = stream
        self.error = error
        self.calls = []

    async def create(self, **kwargs):
        self.calls.append(kwargs)
        if self.error is not None:
            raise self.error
        return self.stream


class FakeGeminiTTSClient:
    def __init__(self, stream=None, error=None):
        self.interactions = FakeInteractions(stream=stream, error=error)
        self.aio = SimpleNamespace(interactions=self.interactions)


def audio_event(payload: bytes, *, mime_type: str = "audio/l16"):
    return SimpleNamespace(
        event_type="step.delta",
        delta=SimpleNamespace(
            type="audio",
            data=base64.b64encode(payload).decode("ascii"),
            mime_type=mime_type,
            channels=1,
            sample_rate=24000,
        ),
    )


def non_audio_event():
    return SimpleNamespace(
        event_type="step.delta",
        delta=SimpleNamespace(type="text", data="not-audio"),
    )


def make_sinhala_smartpbx_pipeline(server):
    transport = FakeTransport()
    pipeline = server.MediaStreamSession(
        websocket=None,
        lang="si",
        media_transport=transport,
        llm_provider="claude",
    )
    pipeline._smartpbx_transfer_context = object()
    return pipeline, transport


def install_gemini_audio_stream(pipeline, payloads):
    pipeline._gemini_tts_client = FakeGeminiTTSClient(
        FakeAsyncStream([audio_event(payload) for payload in payloads])
    )
    return pipeline._gemini_tts_client


@pytest.mark.asyncio
async def test_smartpbx_sinhala_gemini_pcm_is_downsampled_and_completed_once(monkeypatch):
    import server

    pipeline, transport = make_sinhala_smartpbx_pipeline(server)
    monkeypatch.setattr(server, "GEMINI_API_KEY", "test-key")
    pcm24k = bytes(range(256)) * 20
    client = install_gemini_audio_stream(pipeline, [pcm24k[:1537], pcm24k[1537:]])

    await pipeline._speak("සිංහල පිළිතුර", sentence="සිංහල පිළිතුර")

    assert transport.audio
    assert all(isinstance(frame, bytes) and 0 < len(frame) <= 640 for frame in transport.audio)
    assert transport.marks == ["tts_done"]
    assert client.interactions.calls == [{
        "model": "gemini-3.1-flash-tts-preview",
        "input": "සිංහල පිළිතුර",
        "stream": True,
        "response_modalities": ["AUDIO"],
        "response_mime_type": "audio/l16",
        "generation_config": {
            "speech_config": [{
                "language": "si-LK",
                "speaker": "Kore",
                "voice": "Kore",
            }],
        },
        "timeout": 15.0,
    }]


@pytest.mark.asyncio
async def test_smartpbx_sinhala_gemini_failure_never_calls_other_tts(monkeypatch, caplog):
    import server

    pipeline, transport = make_sinhala_smartpbx_pipeline(server)
    text = "private Sinhala text"
    monkeypatch.setattr(server, "GEMINI_API_KEY", "test-key")
    monkeypatch.setattr(
        pipeline, "_tts_openai", AsyncMock(side_effect=AssertionError("no fallback"))
    )
    monkeypatch.setattr(
        pipeline, "_tts_elevenlabs", AsyncMock(side_effect=AssertionError("no fallback"))
    )
    pipeline._gemini_tts_client = FakeGeminiTTSClient(error=TimeoutError())

    with caplog.at_level(logging.ERROR):
        await pipeline._speak(text)

    assert transport.audio == []
    assert transport.marks == []
    assert "provider=gemini outcome=timeout" in caplog.text
    assert text not in caplog.text
    pipeline._tts_openai.assert_not_awaited()
    pipeline._tts_elevenlabs.assert_not_awaited()


@pytest.mark.asyncio
async def test_smartpbx_sinhala_missing_gemini_key_fails_closed_without_client(monkeypatch, caplog):
    import server

    pipeline, transport = make_sinhala_smartpbx_pipeline(server)
    monkeypatch.setattr(server, "GEMINI_API_KEY", "")
    monkeypatch.setattr(
        server,
        "_get_gemini_tts_client",
        lambda: (_ for _ in ()).throw(AssertionError("client must stay lazy")),
        raising=False,
    )

    with caplog.at_level(logging.ERROR):
        await pipeline._speak("private Sinhala text")

    assert transport.audio == []
    assert transport.marks == []
    assert "provider=gemini outcome=missing_api_key" in caplog.text
    assert "private Sinhala text" not in caplog.text


@pytest.mark.asyncio
async def test_smartpbx_sinhala_ignores_non_audio_events_and_never_marks(monkeypatch):
    import server

    pipeline, transport = make_sinhala_smartpbx_pipeline(server)
    monkeypatch.setattr(server, "GEMINI_API_KEY", "test-key")
    pipeline._gemini_tts_client = FakeGeminiTTSClient(FakeAsyncStream([non_audio_event()]))

    await pipeline._speak("සිංහල පිළිතුර")

    assert transport.audio == []
    assert transport.marks == []
    assert pipeline._is_speaking is False


@pytest.mark.asyncio
async def test_smartpbx_sinhala_rejects_malformed_audio_payload_without_mark(monkeypatch, caplog):
    import server

    pipeline, transport = make_sinhala_smartpbx_pipeline(server)
    monkeypatch.setattr(server, "GEMINI_API_KEY", "test-key")
    pipeline._gemini_tts_client = FakeGeminiTTSClient(FakeAsyncStream([
        SimpleNamespace(
            event_type="step.delta",
            delta=SimpleNamespace(type="audio", data="!not-base64!"),
        ),
    ]))

    with caplog.at_level(logging.ERROR):
        await pipeline._speak("private Sinhala text")

    assert transport.audio == []
    assert transport.marks == []
    assert "provider=gemini outcome=malformed_audio" in caplog.text
    assert "private Sinhala text" not in caplog.text


@pytest.mark.asyncio
async def test_smartpbx_sinhala_generation_supersession_sends_no_audio_or_mark(monkeypatch):
    import server

    pipeline, transport = make_sinhala_smartpbx_pipeline(server)
    monkeypatch.setattr(server, "GEMINI_API_KEY", "test-key")
    install_gemini_audio_stream(pipeline, [bytes(range(256)) * 20])
    pipeline._speak_generation = 2

    await pipeline._tts_gemini_sinhala("සිංහල පිළිතුර", turn_generation=1)

    assert transport.audio == []
    assert transport.marks == []
    assert pipeline._is_speaking is False


@pytest.mark.asyncio
async def test_smartpbx_sinhala_sdk_exception_fails_closed_without_mark(monkeypatch, caplog):
    import server

    pipeline, transport = make_sinhala_smartpbx_pipeline(server)
    monkeypatch.setattr(server, "GEMINI_API_KEY", "test-key")
    pipeline._gemini_tts_client = FakeGeminiTTSClient(error=RuntimeError("sdk-secret"))

    with caplog.at_level(logging.ERROR):
        await pipeline._speak("private Sinhala text")

    assert transport.audio == []
    assert transport.marks == []
    assert "provider=gemini outcome=exception" in caplog.text
    assert "sdk-secret" not in caplog.text
    assert "private Sinhala text" not in caplog.text


@pytest.mark.asyncio
async def test_smartpbx_english_still_routes_to_elevenlabs(monkeypatch):
    import server

    transport = FakeTransport()
    pipeline = server.MediaStreamSession(
        websocket=None,
        lang="en",
        media_transport=transport,
        llm_provider="claude",
    )
    pipeline._smartpbx_transfer_context = object()
    elevenlabs = AsyncMock()
    monkeypatch.setattr(pipeline, "_tts_elevenlabs", elevenlabs)
    monkeypatch.setattr(
        pipeline,
        "_tts_gemini_sinhala",
        AsyncMock(side_effect=AssertionError("wrong route")),
        raising=False,
    )

    await pipeline._speak("Hello from Kavya.")

    elevenlabs.assert_awaited_once_with(
        "Hello from Kavya.", sentence=None, turn_generation=-1
    )
    pipeline._tts_gemini_sinhala.assert_not_awaited()
