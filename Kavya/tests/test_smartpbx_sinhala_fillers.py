"""Direct-SmartPBX Sinhala filler latency contract.

The Sinhala path used to give the caller nothing at all while the model
thought, and then serialised its tool filler in front of the PMS call. Both
gaps are audible: production measured a 3.5 s typical first token and one 8 s
initial-response timeout on a single Sinhala call.

These tests pin the two fixes and the property that makes them safe to run at
all: a Sinhala filler never costs a Gemini TTS round trip, because its audio is
rendered once per process and replayed from bytes.
"""

from __future__ import annotations

import asyncio
import base64
from types import SimpleNamespace

import pytest

import server


class FakeTransport:
    def __init__(self) -> None:
        self.audio: list[bytes] = []
        self.marks: list[str] = []
        self.clears = 0

    async def send_audio(self, audio: bytes) -> None:
        self.audio.append(audio)

    async def clear_audio(self) -> int:
        self.clears += 1
        return 0

    async def send_mark(self, name: str) -> None:
        self.marks.append(name)


class _FakeAsyncStream:
    def __init__(self, events):
        self._events = iter(events)

    def __aiter__(self):
        return self

    async def __anext__(self):
        try:
            return next(self._events)
        except StopIteration:
            raise StopAsyncIteration from None


def _audio_event(payload: bytes):
    return SimpleNamespace(
        event_type="step.delta",
        delta=SimpleNamespace(
            type="audio",
            data=base64.b64encode(payload).decode("ascii"),
            mime_type="audio/l16",
            channels=1,
            sample_rate=24000,
        ),
    )


class _FakeInteractions:
    def __init__(self, payloads):
        self.payloads = payloads
        self.calls: list[dict] = []

    async def create(self, **kwargs):
        self.calls.append(kwargs)
        return _FakeAsyncStream([_audio_event(p) for p in self.payloads])


class FakeTTSClient:
    def __init__(self, payloads=(b"\x01\x02" * 2400,)):
        self.interactions = _FakeInteractions(list(payloads))
        self.aio = SimpleNamespace(interactions=self.interactions)


@pytest.fixture(autouse=True)
def _isolated_phrase_cache(monkeypatch):
    """Never let one test's rendered clips leak into the next."""
    monkeypatch.setattr(server, "_SMARTPBX_SINHALA_PHRASE_AUDIO", {})
    monkeypatch.setattr(server, "_SMARTPBX_SINHALA_PHRASE_PREWARM", None)
    monkeypatch.setattr(server, "GEMINI_API_KEY", "test-gemini-key")


def _sinhala_pipeline():
    transport = FakeTransport()
    pipeline = server.MediaStreamSession(
        websocket=None, lang="si", media_transport=transport, llm_provider="gemini",
    )
    pipeline._smartpbx_transfer_context = object()
    return pipeline, transport


# --- the cache itself ------------------------------------------------------

def test_only_fixed_operator_phrases_may_ever_be_cached():
    """Caller transcript and model output must never enter the phrase cache."""
    assert server.SMARTPBX_SINHALA_INITIAL_FILLER_TEXT in server.SMARTPBX_SINHALA_CACHED_PHRASES
    for phrase in server.MEDIA_STREAM_FILLERS["si"].values():
        assert phrase in server.SMARTPBX_SINHALA_CACHED_PHRASES

    server._store_cached_smartpbx_sinhala_phrase_audio("guest said something", b"\xff" * 640)
    assert server._get_cached_smartpbx_sinhala_phrase_audio("guest said something") is None


def test_prewarm_renders_every_fixed_phrase_once_and_only_once(monkeypatch):
    client = FakeTTSClient()
    monkeypatch.setattr(server, "_get_gemini_tts_client", lambda: client)

    asyncio.run(server._prewarm_smartpbx_sinhala_phrase_audio())

    assert server._smartpbx_sinhala_phrase_audio_ready()
    assert len(client.interactions.calls) == len(server.SMARTPBX_SINHALA_CACHED_PHRASES)
    clip = server._get_cached_smartpbx_sinhala_phrase_audio(
        server.SMARTPBX_SINHALA_INITIAL_FILLER_TEXT
    )
    assert clip and len(clip) % 640 == 0

    asyncio.run(server._prewarm_smartpbx_sinhala_phrase_audio())
    assert len(client.interactions.calls) == len(server.SMARTPBX_SINHALA_CACHED_PHRASES)


def test_a_cached_sinhala_phrase_is_spoken_without_any_provider_request(monkeypatch):
    """The whole point: a filler must cost zero synthesis latency."""
    pipeline, transport = _sinhala_pipeline()
    text = server.SMARTPBX_SINHALA_INITIAL_FILLER_TEXT
    clip = b"\x7f" * 1280
    server._store_cached_smartpbx_sinhala_phrase_audio(text, clip)

    def _forbidden():
        raise AssertionError("a cached phrase must not open a Gemini TTS stream")

    monkeypatch.setattr(server, "_get_gemini_tts_client", _forbidden)

    asyncio.run(pipeline._speak(text, sentence=text))

    assert b"".join(transport.audio) == clip
    assert transport.marks == ["tts_done"]


def test_an_uncached_sinhala_reply_still_streams_from_the_provider(monkeypatch):
    """Model speech is not a fixed phrase and must keep its streaming path."""
    pipeline, transport = _sinhala_pipeline()
    client = FakeTTSClient()
    pipeline._gemini_tts_client = client

    asyncio.run(pipeline._speak("සිංහල පිළිතුර", sentence="සිංහල පිළිතුර"))

    assert len(client.interactions.calls) == 1
    assert transport.audio


# --- A1: the initial filler ------------------------------------------------

def test_direct_sinhala_arms_the_initial_filler_once_its_audio_is_rendered():
    pipeline, _transport = _sinhala_pipeline()
    server._store_cached_smartpbx_sinhala_phrase_audio(
        server.SMARTPBX_SINHALA_INITIAL_FILLER_TEXT, b"\xff" * 640,
    )

    async def scenario():
        controller = pipeline._start_initial_smartpbx_filler(
            round_idx=0, generation=pipeline._speak_generation,
        )
        assert controller is not None
        assert controller.text == server.SMARTPBX_SINHALA_INITIAL_FILLER_TEXT
        await controller.on_barge_in()

    asyncio.run(scenario())


def test_direct_sinhala_withholds_the_initial_filler_until_its_audio_exists():
    """An unrendered filler would hold the speak lock through a 2-5 s synthesis."""
    pipeline, _transport = _sinhala_pipeline()

    async def scenario():
        return pipeline._start_initial_smartpbx_filler(
            round_idx=0, generation=pipeline._speak_generation,
        )

    assert asyncio.run(scenario()) is None


def test_direct_sinhala_initial_filler_reuses_the_english_delay_and_semantics():
    """Same controller, same delay knob, same content/tool/barge-in lifecycle."""
    pipeline, _transport = _sinhala_pipeline()
    server._store_cached_smartpbx_sinhala_phrase_audio(
        server.SMARTPBX_SINHALA_INITIAL_FILLER_TEXT, b"\xff" * 640,
    )
    spoken: list[str] = []

    async def scenario():
        async def fake_speak(text, generation=-1, sentence=None):
            spoken.append(text)

        pipeline._invoke_speak = fake_speak
        controller = pipeline._start_initial_smartpbx_filler(
            round_idx=0, generation=pipeline._speak_generation,
        )
        assert controller.delay_seconds == server.SMARTPBX_INITIAL_FILLER_DELAY_SECONDS
        # Content arriving before the delay elapses cancels a filler that has
        # not spoken -- exactly the English rule.
        await controller.on_content_delta()
        assert controller.spoke is False
        assert spoken == []

    asyncio.run(scenario())


def test_twilio_sinhala_media_streams_never_arm_a_smartpbx_initial_filler():
    """The filler belongs to the direct Dialog path only."""
    server._store_cached_smartpbx_sinhala_phrase_audio(
        server.SMARTPBX_SINHALA_INITIAL_FILLER_TEXT, b"\xff" * 640,
    )
    pipeline = server.MediaStreamSession(
        websocket=None, lang="si", media_transport=None, llm_provider="gemini",
    )

    async def scenario():
        return pipeline._start_initial_smartpbx_filler(
            round_idx=0, generation=pipeline._speak_generation,
        )

    assert asyncio.run(scenario()) is None
