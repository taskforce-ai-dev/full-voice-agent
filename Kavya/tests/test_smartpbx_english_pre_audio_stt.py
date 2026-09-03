"""Audit #9: English/ElevenLabs pre-audio window must buffer, not drop.

Mirrors the existing Sinhala/Gemini `_pre_audio_synthesis_active` /
`_handle_pre_audio_stt` coverage (tests/test_smartpbx_sinhala_turntaking.py),
but for the ElevenLabs TTFB window, which previously had no equivalent --
`_is_speaking` was already True the moment the TTS request started, so a
sub-threshold or debounced STT result arriving before the first audio frame
was silently dropped instead of buffered.
"""

from __future__ import annotations

import asyncio

import pytest


class FakeTransport:
    def __init__(self) -> None:
        self.audio: list[bytes] = []
        self.marks: list[str] = []

    async def send_audio(self, audio: bytes) -> None:
        self.audio.append(audio)

    async def send_mark(self, name: str) -> None:
        self.marks.append(name)


class _StreamingResponse:
    """A `httpx` stream response whose body only yields after `release`."""

    status_code = 200

    def __init__(self, chunks, first_chunk_ready: asyncio.Event, release: asyncio.Event):
        self._chunks = chunks
        self.first_chunk_ready = first_chunk_ready
        self.release = release

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return False

    async def aread(self):
        return b""

    async def aiter_bytes(self, chunk_size):
        assert chunk_size == 640
        self.first_chunk_ready.set()
        await self.release.wait()
        for chunk in self._chunks:
            yield chunk


class _StreamingClient:
    def __init__(self, response) -> None:
        self.response = response

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return False

    def stream(self, _method, _url, *, json, headers, timeout):
        return self.response


def _english_pipeline(server):
    transport = FakeTransport()
    pipeline = server.MediaStreamSession(websocket=None, lang="en", media_transport=transport)
    pipeline._smartpbx_transfer_context = object()
    return pipeline, transport


def _configure_english_voice(monkeypatch, server):
    from english_voice_profile import load_kavya_english_voice_profile

    profile = load_kavya_english_voice_profile(
        {"KAVYA_EN_ELEVENLABS_VOICE_ID": "unit-test-canonical-voice"}
    )
    monkeypatch.setattr(server, "ELEVENLABS_API_KEY", "unit-test-api-key")
    monkeypatch.setattr(server, "load_kavya_english_voice_profile", lambda: profile)


@pytest.mark.asyncio
async def test_single_pre_audio_stt_tail_is_buffered_not_dropped_for_english(monkeypatch):
    """The exact Sinhala unit-level contract, ported to English: one
    sub-threshold tail during the pre-audio window is buffered, not
    dispatched immediately, and is admitted on flush."""
    import server

    pipeline, _transport = _english_pipeline(server)
    admitted: list[str] = []

    async def _record(text):
        admitted.append(text)

    monkeypatch.setattr(pipeline, "_accumulate_transcript", _record)
    # Simulates being inside _tts_elevenlabs's pre-audio window (request
    # started, no frame on the wire yet) without needing the full HTTP mock.
    pipeline._is_speaking = True
    pipeline._smartpbx_en_pre_audio_active = True
    pipeline._smartpbx_en_pre_audio_generation = pipeline._speak_generation

    await pipeline._handle_pre_audio_stt("final", "late provider tail")

    assert pipeline._speak_generation == 0
    assert admitted == [], "a single sub-threshold tail must not dispatch immediately"
    await pipeline._flush_pre_audio_stt()
    assert admitted == ["late provider tail"]


@pytest.mark.asyncio
async def test_sustained_pre_audio_stt_barges_in_during_english_ttfb(monkeypatch):
    """Sustained speech during the pre-audio window is a real interruption
    and must still barge in -- buffering is for blips, not for silencing a
    caller who keeps talking through TTFB."""
    import server

    pipeline, _transport = _english_pipeline(server)
    admitted: list[str] = []
    ticks = iter((10.0, 10.3))

    async def _record(text):
        admitted.append(text)

    async def _no_clear(*_args, **_kwargs):
        return 0

    monkeypatch.setattr(server.time, "monotonic", lambda: next(ticks, 10.3))
    monkeypatch.setattr(pipeline, "_accumulate_transcript", _record)
    monkeypatch.setattr(pipeline, "_clear_media_audio", _no_clear)
    pipeline._is_speaking = True
    pipeline._smartpbx_en_pre_audio_active = True
    pipeline._smartpbx_en_pre_audio_generation = pipeline._speak_generation

    await pipeline._handle_pre_audio_stt("interim", "caller continues")
    await pipeline._handle_pre_audio_stt("final", "with more detail")

    assert pipeline._speak_generation == 1
    assert admitted == ["with more detail"]


@pytest.mark.asyncio
async def test_pre_audio_synthesis_active_is_false_outside_any_tts_request(monkeypatch):
    import server

    pipeline, _transport = _english_pipeline(server)
    assert pipeline._pre_audio_synthesis_active() is False


@pytest.mark.asyncio
async def test_pre_audio_window_ends_at_first_chunk_not_at_end_of_utterance(monkeypatch):
    """Regression guard: leaving the pre-audio flag set for the whole
    utterance would route every STT result through pre-audio buffering
    instead of the ordinary barge-in path for as long as speech plays --
    silently disabling barge-in on genuine mid-speech interruptions."""
    import server

    pipeline, transport = _english_pipeline(server)
    _configure_english_voice(monkeypatch, server)
    first_chunk_ready = asyncio.Event()
    release = asyncio.Event()
    response = _StreamingResponse([b"\x00" * 640, b"\x00" * 640], first_chunk_ready, release)
    monkeypatch.setattr(server.httpx, "AsyncClient", lambda: _StreamingClient(response))

    task = asyncio.create_task(pipeline._tts_elevenlabs("A longer non-welcome reply."))
    try:
        await asyncio.wait_for(first_chunk_ready.wait(), timeout=1)
        # Still inside TTFB (aiter_bytes has been entered but nothing yielded
        # yet) -- the pre-audio window must be open.
        assert pipeline._pre_audio_synthesis_active() is True

        release.set()
        await asyncio.wait_for(task, timeout=1)

        # Once audio actually reached the transport, the window must have
        # closed -- not stay open for the rest of the utterance.
        assert pipeline._smartpbx_en_pre_audio_active is False
        assert pipeline._pre_audio_synthesis_active() is False
        assert transport.audio, "the mocked stream must have actually sent audio"
    finally:
        if not task.done():
            release.set()
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
async def test_pre_audio_flush_runs_when_tts_fails_before_any_audio(monkeypatch):
    """A buffered pre-audio tail must not be lost forever if the TTS request
    itself fails before ever emitting a frame."""
    import server

    entered = asyncio.Event()
    release = asyncio.Event()

    class _FailingResponse:
        status_code = 500

        async def __aenter__(self):
            entered.set()
            await release.wait()
            return self

        async def __aexit__(self, *_args):
            return False

        async def aread(self):
            return b"upstream failure"

    class _FailingClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

        def stream(self, _method, _url, *, json, headers, timeout):
            return _FailingResponse()

    pipeline, _transport = _english_pipeline(server)
    _configure_english_voice(monkeypatch, server)
    monkeypatch.setattr(server.httpx, "AsyncClient", lambda: _FailingClient())
    flushed: list[str] = []

    async def _record(text):
        flushed.append(text)

    monkeypatch.setattr(pipeline, "_accumulate_transcript", _record)

    tts_task = asyncio.create_task(pipeline._tts_elevenlabs("A reply that will fail."))
    await asyncio.wait_for(entered.wait(), timeout=1)
    assert pipeline._smartpbx_en_pre_audio_active is True
    await pipeline._handle_pre_audio_stt("final", "caller tail before failure")

    release.set()
    await asyncio.wait_for(tts_task, timeout=1)

    assert flushed == ["caller tail before failure"]
    assert pipeline._smartpbx_en_pre_audio_active is False
