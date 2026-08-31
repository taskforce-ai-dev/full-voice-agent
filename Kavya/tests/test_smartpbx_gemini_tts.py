"""Gemini-only Sinhala TTS contract coverage for Dialog SmartPBX."""

import asyncio
import audioop
import base64
import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
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


class GatedAsyncStream(FakeAsyncStream):
    def __init__(self, events):
        super().__init__(events)
        self.entered = asyncio.Event()
        self.release = asyncio.Event()

    async def __anext__(self):
        self.entered.set()
        await self.release.wait()
        return await super().__anext__()


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


def audio_event(
    payload: bytes,
    *,
    mime_type: str = "audio/l16",
    channels: int = 1,
    sample_rate: int = 24000,
    include_metadata: bool = True,
):
    delta = {
        "type": "audio",
        "data": base64.b64encode(payload).decode("ascii"),
    }
    if include_metadata:
        delta.update(
            mime_type=mime_type,
            channels=channels,
            sample_rate=sample_rate,
        )
    return SimpleNamespace(
        event_type="step.delta",
        delta=SimpleNamespace(**delta),
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


def expected_mulaw_24k_to_8k(payloads):
    ratecv_state = None
    pcm_tail = b""
    expected = b""
    for payload in payloads:
        data = pcm_tail + payload
        if len(data) % 2:
            data, pcm_tail = data[:-1], data[-1:]
        else:
            pcm_tail = b""
        if data:
            pcm8k, ratecv_state = audioop.ratecv(
                data, 2, 1, 24000, 8000, ratecv_state
            )
            expected += audioop.lin2ulaw(pcm8k, 2)
    return expected, pcm_tail


def install_smartpbx_sink(pipeline):
    records = []
    pipeline._smartpbx_diagnostic_sink = lambda stage, outcome, failure: records.append(
        (stage, outcome, failure)
    )
    return records


@pytest.mark.asyncio
async def test_smartpbx_sinhala_gemini_pcm_is_downsampled_and_completed_once(monkeypatch):
    import server

    monkeypatch.setattr(
        server, "SMARTPBX_SINHALA_GEMINI_TTS_VOICE", "Vindemiatrix"
    )
    pipeline, transport = make_sinhala_smartpbx_pipeline(server)
    monkeypatch.setattr(server, "GEMINI_API_KEY", "test-key")
    pcm24k = bytes(range(256)) * 20
    payloads = [pcm24k[:1537], pcm24k[1537:]]
    expected, pcm_tail = expected_mulaw_24k_to_8k(payloads)
    assert pcm_tail == b""
    client = install_gemini_audio_stream(pipeline, payloads)

    await pipeline._speak("සිංහල පිළිතුර", sentence="සිංහල පිළිතුර")

    assert transport.audio
    assert all(isinstance(frame, bytes) and len(frame) == 640 for frame in transport.audio)
    actual = b"".join(transport.audio)
    assert actual[:len(expected)] == expected
    assert actual[len(expected):] == b"\xff" * ((-len(expected)) % 640)
    assert transport.marks == ["tts_done"]
    assert client.interactions.calls == [{
        "model": "gemini-3.1-flash-tts-preview",
        "input": "සිංහල පිළිතුර",
        "stream": True,
        "response_modalities": ["audio"],
        "response_mime_type": "audio/l16",
        "response_format": {
            "type": "audio",
            "mime_type": "audio/l16",
            "sample_rate": 24000,
            "delivery": "inline",
        },
        "generation_config": {
            "speech_config": [{
                "voice": "Vindemiatrix",
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
    records = install_smartpbx_sink(pipeline)

    with caplog.at_level(logging.ERROR):
        await pipeline._speak(text)

    assert transport.audio == []
    assert transport.marks == []
    assert "provider=gemini outcome=timeout" in caplog.text
    assert text not in caplog.text
    pipeline._tts_openai.assert_not_awaited()
    pipeline._tts_elevenlabs.assert_not_awaited()
    assert records == [(
        server.DiagnosticStage.TTS,
        server.DiagnosticOutcome.FAILED,
        server.DiagnosticFailureClass.TTS_TIMEOUT,
    )]


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
    records = install_smartpbx_sink(pipeline)

    with caplog.at_level(logging.ERROR):
        await pipeline._speak("private Sinhala text")

    assert transport.audio == []
    assert transport.marks == []
    assert "provider=gemini outcome=missing_api_key" in caplog.text
    assert "private Sinhala text" not in caplog.text
    assert records == [(
        server.DiagnosticStage.TTS,
        server.DiagnosticOutcome.FAILED,
        server.DiagnosticFailureClass.TTS_MISSING_API_KEY,
    )]


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
    records = install_smartpbx_sink(pipeline)

    with caplog.at_level(logging.ERROR):
        await pipeline._speak("private Sinhala text")

    assert transport.audio == []
    assert transport.marks == []
    assert "provider=gemini outcome=malformed_audio" in caplog.text
    assert "private Sinhala text" not in caplog.text
    assert records == [(
        server.DiagnosticStage.TTS,
        server.DiagnosticOutcome.FAILED,
        server.DiagnosticFailureClass.TTS_EXCEPTION,
    )]


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
async def test_smartpbx_sinhala_http_sdk_failure_is_closed_and_diagnostic(monkeypatch, caplog):
    import server

    pipeline, transport = make_sinhala_smartpbx_pipeline(server)
    monkeypatch.setattr(server, "GEMINI_API_KEY", "test-key")
    request = httpx.Request("POST", "https://gemini.example/interactions")
    response = httpx.Response(503, request=request, content=b"private-response-body")
    pipeline._gemini_tts_client = FakeGeminiTTSClient(
        error=httpx.HTTPStatusError("private-sdk-error", request=request, response=response)
    )
    records = install_smartpbx_sink(pipeline)

    with caplog.at_level(logging.ERROR):
        await pipeline._speak("private Sinhala text")

    assert transport.audio == []
    assert transport.marks == []
    assert "provider=gemini outcome=http_status" in caplog.text
    assert "private-sdk-error" not in caplog.text
    assert "private-response-body" not in caplog.text
    assert "private Sinhala text" not in caplog.text
    assert records == [(
        server.DiagnosticStage.TTS,
        server.DiagnosticOutcome.FAILED,
        server.DiagnosticFailureClass.TTS_HTTP_STATUS,
    )]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "metadata",
    [
        {"mime_type": "audio/mulaw"},
        {"channels": 2},
        {"sample_rate": 8000},
    ],
)
async def test_smartpbx_sinhala_rejects_incompatible_audio_metadata(
    monkeypatch, caplog, metadata
):
    import server

    pipeline, transport = make_sinhala_smartpbx_pipeline(server)
    monkeypatch.setattr(server, "GEMINI_API_KEY", "test-key")
    event = audio_event(bytes(range(256)) * 4)
    for key, value in metadata.items():
        setattr(event.delta, key, value)
    pipeline._gemini_tts_client = FakeGeminiTTSClient(FakeAsyncStream([event]))

    with caplog.at_level(logging.ERROR):
        await pipeline._speak("private Sinhala text")

    assert transport.audio == []
    assert transport.marks == []
    assert "provider=gemini outcome=invalid_audio_metadata" in caplog.text
    assert "private Sinhala text" not in caplog.text


@pytest.mark.asyncio
async def test_smartpbx_sinhala_accepts_omitted_audio_metadata(monkeypatch):
    import server

    pipeline, transport = make_sinhala_smartpbx_pipeline(server)
    monkeypatch.setattr(server, "GEMINI_API_KEY", "test-key")
    event = audio_event(bytes(range(256)) * 4, include_metadata=False)
    pipeline._gemini_tts_client = FakeGeminiTTSClient(FakeAsyncStream([event]))

    await pipeline._speak("සිංහල පිළිතුර")

    assert transport.audio
    assert all(len(frame) == 640 for frame in transport.audio)
    assert transport.marks == ["tts_done"]


@pytest.mark.asyncio
async def test_smartpbx_sinhala_ignores_uri_only_audio_delta(monkeypatch):
    import server

    pipeline, transport = make_sinhala_smartpbx_pipeline(server)
    monkeypatch.setattr(server, "GEMINI_API_KEY", "test-key")
    pipeline._gemini_tts_client = FakeGeminiTTSClient(FakeAsyncStream([
        SimpleNamespace(
            event_type="step.delta",
            delta=SimpleNamespace(type="audio", uri="gs://private/audio"),
        ),
    ]))

    await pipeline._speak("සිංහල පිළිතුර")

    assert transport.audio == []
    assert transport.marks == []


@pytest.mark.asyncio
async def test_smartpbx_sinhala_rejects_odd_final_pcm_tail(monkeypatch, caplog):
    import server

    pipeline, transport = make_sinhala_smartpbx_pipeline(server)
    monkeypatch.setattr(server, "GEMINI_API_KEY", "test-key")
    pipeline._gemini_tts_client = FakeGeminiTTSClient(FakeAsyncStream([
        audio_event((bytes(range(256)) * 4) + b"\x00"),
    ]))

    with caplog.at_level(logging.ERROR):
        await pipeline._speak("private Sinhala text")

    assert transport.audio == []
    assert transport.marks == []
    assert "provider=gemini outcome=malformed_audio" in caplog.text
    assert "private Sinhala text" not in caplog.text


@pytest.mark.asyncio
async def test_sinhala_smartpbx_stale_runner_cannot_send_after_supersession(monkeypatch):
    import server

    pipeline, transport = make_sinhala_smartpbx_pipeline(server)
    monkeypatch.setattr(server, "GEMINI_API_KEY", "test-key")
    stream = GatedAsyncStream([audio_event(bytes(range(256)) * 4)])
    pipeline._gemini_tts_client = FakeGeminiTTSClient(stream)
    pipeline._active_smartpbx_turn_id = "old-turn"
    runner = server._SmartPBXRunnerContext(
        turn_id="old-turn",
        dropped_frame_baseline=0,
        speak_generation=pipeline._speak_generation,
        raw_utterance="",
    )
    token = server._smartpbx_runner_context.set(runner)
    try:
        task = asyncio.create_task(pipeline._tts_gemini_sinhala("සිංහල පිළිතුර"))
        await stream.entered.wait()
        pipeline._active_smartpbx_turn_id = "new-turn"
        stream.release.set()
        await task
    finally:
        server._smartpbx_runner_context.reset(token)

    assert transport.audio == []
    assert transport.marks == []


@pytest.mark.asyncio
async def test_smartpbx_sinhala_cancellation_cleans_speaking_without_media(monkeypatch):
    import server

    pipeline, transport = make_sinhala_smartpbx_pipeline(server)
    monkeypatch.setattr(server, "GEMINI_API_KEY", "test-key")
    stream = GatedAsyncStream([audio_event(bytes(range(256)) * 4)])
    pipeline._gemini_tts_client = FakeGeminiTTSClient(stream)

    task = asyncio.create_task(pipeline._tts_gemini_sinhala("සිංහල පිළිතුර"))
    await stream.entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert task.done()
    assert transport.audio == []
    assert transport.marks == []
    assert pipeline._is_speaking is False


@pytest.mark.asyncio
async def test_twilio_sinhala_failure_does_not_emit_smartpbx_tts_diagnostic(monkeypatch):
    import server

    pipeline = server.MediaStreamSession(
        websocket=None,
        lang="si",
        media_transport=FakeTransport(),
        llm_provider="claude",
    )
    records = install_smartpbx_sink(pipeline)
    monkeypatch.setattr(server, "GEMINI_API_KEY", "")

    await pipeline._tts_gemini_sinhala("සිංහල පිළිතුර")

    assert records == []


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
