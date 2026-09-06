"""Gemini-only Sinhala TTS contract coverage for Dialog SmartPBX."""

import asyncio
import audioop
import base64
import logging
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from smartpbx_protocol import CallContext, MediaFormat
from smartpbx_session import KavyaSmartPBXSession


@pytest.fixture(autouse=True)
def _isolated_sinhala_tts_process_state(monkeypatch):
    """Never let one test's cached clips or quota streak leak into another."""
    import server

    monkeypatch.setattr(server, "_SMARTPBX_SINHALA_PHRASE_AUDIO", {})
    monkeypatch.setattr(
        server,
        "_smartpbx_sinhala_tts_quota_state",
        {"consecutive_failures": 0, "degraded": False, "degraded_logged": False},
    )
    monkeypatch.setattr(
        server,
        "_smartpbx_sinhala_tts_model_state",
        {"exhausted_until": {}, "active_model": None},
    )


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
    def __init__(self, stream=None, error=None, stream_factory=None):
        self.stream = stream
        self.error = error
        # A model-fallback attempt calls `create()` again for the next model;
        # a fixed single-use `stream` would be exhausted on the retry (fine
        # for every non-fallback test, which calls `create()` once). Pass
        # `stream_factory` to build a fresh stream per call for a test that
        # exercises more than one attempt.
        self.stream_factory = stream_factory
        self.calls = []

    async def create(self, **kwargs):
        self.calls.append(kwargs)
        if self.error is not None:
            raise self.error
        if self.stream_factory is not None:
            return self.stream_factory()
        return self.stream


class FakeGeminiTTSClient:
    def __init__(self, stream=None, error=None, stream_factory=None):
        self.interactions = FakeInteractions(stream=stream, error=error, stream_factory=stream_factory)
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


def interaction_lifecycle_event(event_type):
    """A terminal/lifecycle event with no error -- must never be a failure."""
    return SimpleNamespace(event_type=event_type, error=None)


def error_event(code, *, status=None, message="ignored provider message"):
    """The live-incident shape: an `error` event carrying `.error.code`."""
    return SimpleNamespace(
        event_type="error",
        error=SimpleNamespace(code=code, status=status, message=message),
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


class ActivatedSelectionStt:
    def __init__(self):
        self.starts = 0
        self.stops = 0

    def start(self):
        self.starts += 1

    def stop(self):
        self.stops += 1

    def feed(self, _payload):
        return None


def _selection_context():
    return CallContext(
        call_id="media-leg", other_leg_call_id="dialog-call",
        caller_id_number="+94000000000", callee_id_number="+94110000000",
        account_id="dialog-account", media_format=MediaFormat(encoding="g711_ulaw", sample_rate=8000),
    )


def _real_selection_session(server):
    transport = FakeTransport()
    pipeline = server.MediaStreamSession(
        websocket=None, lang="en", media_transport=transport,
        anthropic_client=object(), llm_provider="claude", model="test-model",
    )
    stt = ActivatedSelectionStt()
    session = KavyaSmartPBXSession(
        _selection_context(), transport, pipeline=pipeline,
        stt_factory=lambda **_kwargs: stt,
        post_call_processor=lambda **_kwargs: asyncio.sleep(0), welcome_text="",
        llm_provider="claude", model="test-model",
    )
    return session, pipeline, stt


@pytest.mark.asyncio
async def test_real_selected_profiles_keep_tts_routing_owned_by_lang(monkeypatch):
    import server

    english_session, english, english_stt = _real_selection_session(server)
    sinhala_session, sinhala, sinhala_stt = _real_selection_session(server)
    routes: list[tuple[str, str]] = []
    canonical = SimpleNamespace(voice_id="canonical", model_id="canonical-model", request_voice_settings={})

    async def quiet_tts(_text, **_kwargs):
        return None

    async def elevenlabs(text, **_kwargs):
        assert server.load_kavya_english_voice_profile() is canonical
        routes.append(("elevenlabs", text))

    async def gemini(text, **_kwargs):
        routes.append(("gemini", text))

    monkeypatch.setattr(english, "_tts_elevenlabs", quiet_tts)
    monkeypatch.setattr(english, "_tts_gemini_sinhala", quiet_tts)
    monkeypatch.setattr(sinhala, "_tts_elevenlabs", quiet_tts)
    monkeypatch.setattr(sinhala, "_tts_gemini_sinhala", quiet_tts)
    monkeypatch.setattr(
        server, "load_kavya_english_voice_profile",
        lambda: (_ for _ in ()).throw(AssertionError("selection must not load TTS secrets")),
    )
    monkeypatch.setattr(server, "SMARTPBX_SINHALA_LLM_PROVIDER", "gemini")
    monkeypatch.setattr(server, "get_tools_gemini", lambda: [])
    sinhala.gemini_client = object()

    await asyncio.gather(english_session.start(), sinhala_session.start())
    await asyncio.gather(english_session.feed_dtmf("1"), sinhala_session.feed_dtmf("2"))
    assert (english_stt.starts, sinhala_stt.starts) == (1, 1)

    monkeypatch.setattr(server, "load_kavya_english_voice_profile", lambda: canonical)
    monkeypatch.setattr(english, "_tts_elevenlabs", elevenlabs)
    monkeypatch.setattr(english, "_tts_gemini_sinhala", gemini)
    monkeypatch.setattr(sinhala, "_tts_elevenlabs", elevenlabs)
    monkeypatch.setattr(sinhala, "_tts_gemini_sinhala", gemini)

    await english._speak("English route")
    await sinhala._speak("සිංහල Gemini route")
    # A Gemini LLM rollback changes model execution, not the selected Sinhala TTS router.
    sinhala.llm_provider = "claude"
    await sinhala._speak("සිංහල Claude fallback route")

    assert routes == [
        ("elevenlabs", "English route"),
        ("gemini", "සිංහල Gemini route"),
        ("gemini", "සිංහල Claude fallback route"),
    ]


@pytest.mark.asyncio
async def test_real_preselection_menu_bypasses_live_tts(monkeypatch):
    import server

    session, pipeline, _stt = _real_selection_session(server)
    routes: list[tuple[str, str]] = []

    async def elevenlabs(text, **_kwargs):
        routes.append(("elevenlabs", text))

    async def gemini(text, **_kwargs):
        routes.append(("gemini", text))

    monkeypatch.setattr(pipeline, "_tts_elevenlabs", elevenlabs)
    monkeypatch.setattr(pipeline, "_tts_gemini_sinhala", gemini)
    await session.start()
    await asyncio.sleep(0)

    assert routes == []
    assert len(session._transport.audio) == 1
    assert session._transport.marks == ["language-menu"]


@pytest.mark.asyncio
async def test_selected_language_keeps_tts_routing_owned_by_lang(monkeypatch):
    """LLM fallback must not redirect selected Sinhala speech to ElevenLabs."""
    import server

    english = server.MediaStreamSession(
        websocket=None, lang="en", media_transport=FakeTransport(), llm_provider="claude",
    )
    english._smartpbx_transfer_context = object()
    sinhala = server.MediaStreamSession(
        websocket=None, lang="si", media_transport=FakeTransport(), llm_provider="claude",
    )
    sinhala._smartpbx_transfer_context = object()
    calls: list[tuple[str, str]] = []

    async def elevenlabs(text, **_kwargs):
        calls.append(("elevenlabs", text))

    async def gemini(text, **_kwargs):
        calls.append(("gemini", text))

    monkeypatch.setattr(english, "_tts_elevenlabs", elevenlabs)
    monkeypatch.setattr(english, "_tts_gemini_sinhala", gemini)
    monkeypatch.setattr(sinhala, "_tts_elevenlabs", elevenlabs)
    monkeypatch.setattr(sinhala, "_tts_gemini_sinhala", gemini)

    await english._speak("English route")
    await sinhala._speak("සිංහල")

    assert calls == [("elevenlabs", "English route"), ("gemini", "සිංහල")]


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
    assert client.aio.interactions.calls == [{
        "model": "gemini-3.1-flash-tts-preview",
        "input": "සිංහල පිළිතුර",
        "stream": True,
        "response_format": {"type": "audio"},
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
@pytest.mark.parametrize("credential", ["", " \t\n "])
async def test_smartpbx_sinhala_missing_gemini_key_fails_closed_without_client(
    monkeypatch, caplog, credential,
):
    import server

    pipeline, transport = make_sinhala_smartpbx_pipeline(server)
    monkeypatch.setattr(server, "GEMINI_API_KEY", credential)
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


# --- error-event classification (2026-09-04 quota_exceeded incident) -------

@pytest.mark.asyncio
async def test_iterator_raises_typed_error_for_the_live_quota_exceeded_shape():
    """Fake stream shaped exactly like the 06:19-06:22 UTC production incident:
    interaction.created -> interaction.status_update -> error(code=quota_exceeded),
    never any step.delta audio event."""
    import server

    stream = FakeAsyncStream([
        interaction_lifecycle_event("interaction.created"),
        interaction_lifecycle_event("interaction.status_update"),
        error_event("quota_exceeded", message="You exceeded your current quota"),
    ])

    with pytest.raises(server._GeminiTTSProviderError) as excinfo:
        async for _ in server._iter_gemini_tts_audio_deltas(stream):
            pass

    assert excinfo.value.code == "quota_exceeded"


@pytest.mark.asyncio
async def test_iterator_raises_for_a_terminal_interaction_event_carrying_an_error():
    """Defensive coverage: a terminal interaction.* event with a nested error
    must be surfaced too, not just the dedicated `error` event type."""
    import server

    stream = FakeAsyncStream([
        interaction_lifecycle_event("interaction.created"),
        SimpleNamespace(
            event_type="interaction.completed",
            error=SimpleNamespace(code="server_error", status=None, message="boom"),
        ),
    ])

    with pytest.raises(server._GeminiTTSProviderError) as excinfo:
        async for _ in server._iter_gemini_tts_audio_deltas(stream):
            pass

    assert excinfo.value.code == "server_error"


@pytest.mark.asyncio
async def test_iterator_never_raises_for_lifecycle_events_without_an_error():
    import server

    stream = FakeAsyncStream([
        interaction_lifecycle_event("interaction.created"),
        interaction_lifecycle_event("interaction.status_update"),
        interaction_lifecycle_event("interaction.completed"),
    ])

    deltas = [item async for item in server._iter_gemini_tts_audio_deltas(stream)]

    assert deltas == []


@pytest.mark.parametrize(
    "code,status,expected",
    [
        ("quota_exceeded", None, "quota_exceeded"),
        ("rate_limit_exceeded", None, "rate_limited"),
        ("invalid_request_error", None, "invalid_request"),
        ("permission_denied", None, "permission_denied"),
        ("internal_server_error", None, "server_error"),
        (None, "RESOURCE_EXHAUSTED", "rate_limited"),
        (None, "PERMISSION_DENIED", "permission_denied"),
        (None, "INVALID_ARGUMENT", "invalid_request"),
        (None, "UNAVAILABLE", "server_error"),
        ("something_else_entirely", None, "unknown_provider_error"),
        (None, None, "unknown_provider_error"),
    ],
)
def test_classify_gemini_tts_provider_error_maps_to_the_closed_vocabulary(
    code, status, expected,
):
    import server

    error = SimpleNamespace(code=code, status=status, message="never read")
    assert server._classify_gemini_tts_provider_error(error) == expected


def test_gemini_tts_provider_error_narrows_an_unrecognized_code():
    import server

    assert server._GeminiTTSProviderError("not-a-real-code").code == "unknown_provider_error"
    assert server._GeminiTTSProviderError("quota_exceeded").code == "quota_exceeded"


@pytest.mark.asyncio
async def test_smartpbx_sinhala_quota_exceeded_logs_outcome_not_empty_audio(
    monkeypatch, caplog,
):
    """The core misclassification bug: a quota error must never read as
    empty_audio, and must emit the closest DiagnosticFailureClass, TTS_QUOTA."""
    import server

    pipeline, transport = make_sinhala_smartpbx_pipeline(server)
    monkeypatch.setattr(server, "GEMINI_API_KEY", "test-key")
    # Every model in the configured fallback chain sees the identical
    # quota_exceeded shape, so the whole chain exhausts to a final failure.
    pipeline._gemini_tts_client = FakeGeminiTTSClient(stream_factory=lambda: FakeAsyncStream([
        interaction_lifecycle_event("interaction.created"),
        interaction_lifecycle_event("interaction.status_update"),
        error_event("quota_exceeded"),
    ]))
    records = install_smartpbx_sink(pipeline)

    with caplog.at_level(logging.ERROR):
        await pipeline._tts_gemini_sinhala("private Sinhala text")

    assert "provider=gemini outcome=quota_exceeded" in caplog.text
    assert "outcome=empty_audio" not in caplog.text
    assert "private Sinhala text" not in caplog.text
    assert (
        server.DiagnosticStage.TTS,
        server.DiagnosticOutcome.FAILED,
        server.DiagnosticFailureClass.TTS_QUOTA,
    ) in records


@pytest.mark.asyncio
async def test_smartpbx_sinhala_non_quota_provider_errors_never_fall_back(monkeypatch):
    """invalid_request/permission_denied/server_error/unknown must never
    trigger a retry on the next model -- only one `create()` call, ever."""
    import server

    pipeline, _transport = make_sinhala_smartpbx_pipeline(server)
    monkeypatch.setattr(server, "GEMINI_API_KEY", "test-key")
    client = FakeGeminiTTSClient(FakeAsyncStream([
        error_event("invalid_request_error"),
    ]))
    pipeline._gemini_tts_client = client
    records = install_smartpbx_sink(pipeline)

    await pipeline._tts_gemini_sinhala("private Sinhala text")

    assert len(client.aio.interactions.calls) == 1
    assert client.aio.interactions.calls[0]["model"] == server.SMARTPBX_SINHALA_GEMINI_TTS_MODEL
    assert (
        server.DiagnosticStage.TTS,
        server.DiagnosticOutcome.FAILED,
        server.DiagnosticFailureClass.TTS_PROVIDER_ERROR,
    ) in records


@pytest.mark.asyncio
async def test_smartpbx_sinhala_genuinely_empty_stream_still_logs_empty_audio(
    monkeypatch, caplog,
):
    """A stream that completes with no audio and no error event is a real
    empty_audio outcome -- must not be swept into the new provider-error path."""
    import server

    pipeline, transport = make_sinhala_smartpbx_pipeline(server)
    monkeypatch.setattr(server, "GEMINI_API_KEY", "test-key")
    pipeline._gemini_tts_client = FakeGeminiTTSClient(FakeAsyncStream([
        interaction_lifecycle_event("interaction.created"),
        interaction_lifecycle_event("interaction.completed"),
    ]))
    records = install_smartpbx_sink(pipeline)

    with caplog.at_level(logging.ERROR):
        await pipeline._tts_gemini_sinhala("private Sinhala text")

    assert transport.audio == []
    assert "provider=gemini outcome=empty_audio" in caplog.text
    assert (
        server.DiagnosticStage.TTS,
        server.DiagnosticOutcome.FAILED,
        server.DiagnosticFailureClass.TTS_EXCEPTION,
    ) in records


# --- sticky quota degradation signal ----------------------------------------

def test_status_flips_after_n_consecutive_quota_failures_and_resets_on_success(
    monkeypatch,
):
    import server

    monkeypatch.setattr(server, "SMARTPBX_SINHALA_TTS_QUOTA_STICKY_AFTER", 3)

    assert server._smartpbx_sinhala_tts_degraded() is False
    server._note_smartpbx_sinhala_tts_quota_failure()
    assert server._smartpbx_sinhala_tts_degraded() is False
    server._note_smartpbx_sinhala_tts_quota_failure()
    assert server._smartpbx_sinhala_tts_degraded() is False
    server._note_smartpbx_sinhala_tts_quota_failure()
    assert server._smartpbx_sinhala_tts_degraded() is True

    server._note_smartpbx_sinhala_tts_synthesis_success()
    assert server._smartpbx_sinhala_tts_degraded() is False


def test_sinhala_tts_quota_degraded_warning_logs_exactly_once(monkeypatch, caplog):
    import server

    monkeypatch.setattr(server, "SMARTPBX_SINHALA_TTS_QUOTA_STICKY_AFTER", 2)

    with caplog.at_level(logging.WARNING):
        server._note_smartpbx_sinhala_tts_quota_failure()
        server._note_smartpbx_sinhala_tts_quota_failure()
        server._note_smartpbx_sinhala_tts_quota_failure()

    assert caplog.text.count("event=sinhala_tts_quota_degraded") == 1


def test_smartpbx_status_json_exposes_sinhala_tts_degraded_flag(monkeypatch):
    import server

    monkeypatch.setattr(server, "SMARTPBX_SINHALA_TTS_QUOTA_STICKY_AFTER", 1)
    app = server.build_service_app("smartpbx", {
        "ENABLE_SMARTPBX_WSS": "true",
        "SMARTPBX_WS_TOKEN": "status-token",
        "SMARTPBX_ACCOUNT_ID": "account-1",
    })
    status = {route.path: route for route in app.routes}["/smartpbx/status"].endpoint
    request = SimpleNamespace(headers={"X-Kavya-SmartPBX-Token": "status-token"})

    assert status(request)["sinhala_tts_degraded"] is False

    server._note_smartpbx_sinhala_tts_quota_failure()
    assert status(request)["sinhala_tts_degraded"] is True

    server._note_smartpbx_sinhala_tts_synthesis_success()
    assert status(request)["sinhala_tts_degraded"] is False


@pytest.mark.asyncio
async def test_live_gemini_quota_failure_advances_the_sticky_counter_end_to_end(
    monkeypatch,
):
    """Wired all the way through `_tts_gemini_sinhala`, not just the helper."""
    import server

    monkeypatch.setattr(server, "SMARTPBX_SINHALA_TTS_QUOTA_STICKY_AFTER", 1)
    pipeline, _transport = make_sinhala_smartpbx_pipeline(server)
    monkeypatch.setattr(server, "GEMINI_API_KEY", "test-key")
    # Every model in the chain hits the same error, so the whole chain
    # exhausts to the sticky-degraded-triggering final failure.
    pipeline._gemini_tts_client = FakeGeminiTTSClient(
        stream_factory=lambda: FakeAsyncStream([error_event("quota_exceeded")])
    )

    assert server._smartpbx_sinhala_tts_degraded() is False
    await pipeline._tts_gemini_sinhala("private Sinhala text")
    assert server._smartpbx_sinhala_tts_degraded() is True

    # A subsequent genuine success clears the degraded signal. Model
    # exhaustion (a separate, sticky-until-quota-reset mechanism, covered by
    # its own tests) is reset here so this test isolates the degraded-signal
    # contract from it.
    server._smartpbx_sinhala_tts_model_state["exhausted_until"].clear()
    pipeline._gemini_tts_client = FakeGeminiTTSClient(
        FakeAsyncStream([audio_event(bytes(range(256)) * 4)])
    )
    await pipeline._tts_gemini_sinhala("සිංහල පිළිතුර")
    assert server._smartpbx_sinhala_tts_degraded() is False


# --- quota-aware Gemini Sinhala TTS model fallback chain --------------------

def test_smartpbx_sinhala_tts_model_chain_is_primary_then_fallbacks_deduplicated(
    monkeypatch,
):
    import server

    monkeypatch.setattr(server, "SMARTPBX_SINHALA_GEMINI_TTS_MODEL", "primary-model")
    monkeypatch.setattr(
        server, "SMARTPBX_SINHALA_GEMINI_TTS_FALLBACK_MODELS",
        ("primary-model", "fallback-a", "fallback-b"),
    )

    assert server._smartpbx_sinhala_tts_model_chain() == (
        "primary-model", "fallback-a", "fallback-b",
    )


def test_parse_smartpbx_sinhala_tts_fallback_models_env_validation():
    import server

    default = ("gemini-2.5-flash-preview-tts", "gemini-2.5-pro-preview-tts")
    assert server._parse_smartpbx_sinhala_tts_fallback_models("") == default
    assert server._parse_smartpbx_sinhala_tts_fallback_models("   ") == default
    assert server._parse_smartpbx_sinhala_tts_fallback_models("model-a,model-b") == (
        "model-a", "model-b",
    )
    assert server._parse_smartpbx_sinhala_tts_fallback_models(" model-a , model-b ") == (
        "model-a", "model-b",
    )
    # Invalid names (uppercase, underscore, spaces, empty segments) are
    # dropped individually; an all-invalid list falls back to the default
    # rather than silently disabling fallback.
    assert server._parse_smartpbx_sinhala_tts_fallback_models("Bad_Model,model-ok,,") == (
        "model-ok",
    )
    assert server._parse_smartpbx_sinhala_tts_fallback_models("INVALID_ONLY") == default


@pytest.mark.asyncio
async def test_quota_on_primary_falls_back_to_secondary_with_identical_voice_and_text(
    monkeypatch, caplog,
):
    import server

    pipeline, transport = make_sinhala_smartpbx_pipeline(server)
    monkeypatch.setattr(server, "GEMINI_API_KEY", "test-key")
    text = "සිංහල පිළිතුර"
    payload = bytes(range(256)) * 4

    class SwitchingInteractions:
        def __init__(self):
            self.calls = []

        async def create(self, **kwargs):
            self.calls.append(kwargs)
            if kwargs["model"] == server.SMARTPBX_SINHALA_GEMINI_TTS_MODEL:
                return FakeAsyncStream([error_event("quota_exceeded")])
            return FakeAsyncStream([audio_event(payload)])

    interactions = SwitchingInteractions()
    pipeline._gemini_tts_client = SimpleNamespace(aio=SimpleNamespace(interactions=interactions))

    with caplog.at_level(logging.WARNING):
        await pipeline._tts_gemini_sinhala(text, sentence=text)

    calls = interactions.calls
    assert len(calls) == 2
    assert calls[0]["model"] == server.SMARTPBX_SINHALA_GEMINI_TTS_MODEL
    assert calls[1]["model"] == server.SMARTPBX_SINHALA_GEMINI_TTS_FALLBACK_MODELS[0]
    # Identical text and voice config on both attempts -- only the model differs.
    assert calls[0]["input"] == calls[1]["input"] == text
    assert calls[0]["generation_config"] == calls[1]["generation_config"]
    assert calls[0]["response_format"] == calls[1]["response_format"]
    assert transport.audio, "audio must reach the wire via the secondary model"
    assert transport.marks == ["tts_done"]
    assert (
        f"event=sinhala_tts_model_fallback from={server.SMARTPBX_SINHALA_GEMINI_TTS_MODEL} "
        f"to={server.SMARTPBX_SINHALA_GEMINI_TTS_FALLBACK_MODELS[0]} reason=quota_exceeded"
        in caplog.text
    )
    assert server._smartpbx_sinhala_tts_model_is_exhausted(server.SMARTPBX_SINHALA_GEMINI_TTS_MODEL)
    assert pipeline._smartpbx_tts_model_fallbacks_total == 1


def test_exhausted_model_is_skipped_until_the_utc_reset_boundary(monkeypatch):
    import server

    monkeypatch.setattr(server, "SMARTPBX_SINHALA_GEMINI_TTS_MODEL", "primary")
    monkeypatch.setattr(server, "SMARTPBX_SINHALA_GEMINI_TTS_FALLBACK_MODELS", ("secondary",))
    monkeypatch.setattr(server, "SMARTPBX_SINHALA_TTS_MODEL_RESET_UTC_HOUR", 7)

    before_reset = datetime(2026, 9, 4, 6, 0, tzinfo=timezone.utc)
    server._mark_smartpbx_sinhala_tts_model_exhausted("primary", now=before_reset)

    just_before = datetime(2026, 9, 4, 6, 59, tzinfo=timezone.utc)
    assert server._smartpbx_sinhala_tts_model_is_exhausted("primary", now=just_before)
    assert server._smartpbx_sinhala_tts_available_models(now=just_before) == ["secondary"]

    at_reset = datetime(2026, 9, 4, 7, 0, tzinfo=timezone.utc)
    assert not server._smartpbx_sinhala_tts_model_is_exhausted("primary", now=at_reset)
    assert server._smartpbx_sinhala_tts_available_models(now=at_reset) == ["primary", "secondary"]


def test_a_failure_after_the_reset_hour_is_exhausted_until_the_next_day(monkeypatch):
    import server

    monkeypatch.setattr(server, "SMARTPBX_SINHALA_TTS_MODEL_RESET_UTC_HOUR", 7)
    after_reset = datetime(2026, 9, 4, 8, 30, tzinfo=timezone.utc)

    boundary = server._smartpbx_sinhala_tts_quota_reset_boundary(after_reset)

    assert boundary == datetime(2026, 9, 5, 7, 0, tzinfo=timezone.utc)


def test_a_failure_before_the_reset_hour_is_exhausted_only_until_today(monkeypatch):
    import server

    monkeypatch.setattr(server, "SMARTPBX_SINHALA_TTS_MODEL_RESET_UTC_HOUR", 7)
    before_reset = datetime(2026, 9, 4, 5, 0, tzinfo=timezone.utc)

    boundary = server._smartpbx_sinhala_tts_quota_reset_boundary(before_reset)

    assert boundary == datetime(2026, 9, 4, 7, 0, tzinfo=timezone.utc)


@pytest.mark.asyncio
async def test_all_models_exhausted_upfront_skips_straight_to_the_apology(monkeypatch):
    import server

    pipeline, transport = make_sinhala_smartpbx_pipeline(server)
    monkeypatch.setattr(server, "GEMINI_API_KEY", "test-key")
    server._store_cached_smartpbx_sinhala_phrase_audio(
        server.SMARTPBX_SINHALA_TTS_UNAVAILABLE_TEXT, b"\xff" * 640,
    )
    for model in server._smartpbx_sinhala_tts_model_chain():
        server._mark_smartpbx_sinhala_tts_model_exhausted(model)

    def _forbidden_client():
        raise AssertionError("must not build a client when the whole chain is exhausted")

    monkeypatch.setattr(server, "_get_gemini_tts_client", _forbidden_client)

    await pipeline._tts_gemini_sinhala("private Sinhala text")

    assert b"".join(transport.audio) == b"\xff" * 640
    assert transport.marks == ["tts_done"]


def test_smartpbx_status_json_exposes_the_active_sinhala_tts_model(monkeypatch):
    import server

    app = server.build_service_app("smartpbx", {
        "ENABLE_SMARTPBX_WSS": "true",
        "SMARTPBX_WS_TOKEN": "status-token",
        "SMARTPBX_ACCOUNT_ID": "account-1",
    })
    status = {route.path: route for route in app.routes}["/smartpbx/status"].endpoint
    request = SimpleNamespace(headers={"X-Kavya-SmartPBX-Token": "status-token"})

    assert status(request)["sinhala_tts_model"] == server.SMARTPBX_SINHALA_GEMINI_TTS_MODEL

    server._note_smartpbx_sinhala_tts_active_model("gemini-2.5-flash-preview-tts")
    assert status(request)["sinhala_tts_model"] == "gemini-2.5-flash-preview-tts"


# --- text-specific retry on a NON-primary model (2026-09-04 live evidence) -

class ModelRoutedInteractions:
    """Route each `create()` call to a per-model stream factory."""

    def __init__(self, by_model):
        self.by_model = by_model
        self.calls = []

    async def create(self, **kwargs):
        self.calls.append(kwargs)
        return self.by_model[kwargs["model"]]()


def _model_routed_client(by_model):
    return SimpleNamespace(aio=SimpleNamespace(interactions=ModelRoutedInteractions(by_model)))


@pytest.mark.asyncio
async def test_invalid_request_on_a_fallback_model_retries_same_text_next_model(
    monkeypatch, caplog,
):
    """The live-incident shape: a non-primary model rejects short Sinhala
    text outright -- retry the SAME text on the next model, never marking
    the model the caller heard nothing from as exhausted."""
    import server

    monkeypatch.setattr(server, "SMARTPBX_SINHALA_GEMINI_TTS_MODEL", "primary")
    monkeypatch.setattr(
        server, "SMARTPBX_SINHALA_GEMINI_TTS_FALLBACK_MODELS", ("secondary", "tertiary"),
    )
    server._mark_smartpbx_sinhala_tts_model_exhausted("primary")
    pipeline, transport = make_sinhala_smartpbx_pipeline(server)
    monkeypatch.setattr(server, "GEMINI_API_KEY", "test-key")
    payload = bytes(range(256)) * 4
    client = _model_routed_client({
        "secondary": lambda: FakeAsyncStream([error_event("invalid_request_error")]),
        "tertiary": lambda: FakeAsyncStream([audio_event(payload)]),
    })
    pipeline._gemini_tts_client = client

    with caplog.at_level(logging.WARNING):
        await pipeline._tts_gemini_sinhala("ආයුබෝවන්.")

    calls = client.aio.interactions.calls
    assert [c["model"] for c in calls] == ["secondary", "tertiary"]
    assert transport.audio, "audio must reach the wire via the retried model"
    assert transport.marks == ["tts_done"]
    assert (
        "event=sinhala_tts_model_retry from=secondary to=tertiary "
        "reason=invalid_request" in caplog.text
    )
    # Text-specific -- never mark the model exhausted for this.
    assert server._smartpbx_sinhala_tts_model_is_exhausted("secondary") is False
    assert server._smartpbx_sinhala_tts_model_is_exhausted("tertiary") is False
    assert pipeline._smartpbx_tts_model_retries_total == 1
    assert pipeline._smartpbx_tts_model_fallbacks_total == 0


@pytest.mark.asyncio
async def test_empty_audio_on_a_fallback_model_retries_same_text_next_model(
    monkeypatch, caplog,
):
    """The other live-incident shape: a completed stream with zero audio
    deltas and no error on a non-primary model."""
    import server

    monkeypatch.setattr(server, "SMARTPBX_SINHALA_GEMINI_TTS_MODEL", "primary")
    monkeypatch.setattr(
        server, "SMARTPBX_SINHALA_GEMINI_TTS_FALLBACK_MODELS", ("secondary", "tertiary"),
    )
    server._mark_smartpbx_sinhala_tts_model_exhausted("primary")
    pipeline, transport = make_sinhala_smartpbx_pipeline(server)
    monkeypatch.setattr(server, "GEMINI_API_KEY", "test-key")
    payload = bytes(range(256)) * 4
    client = _model_routed_client({
        "secondary": lambda: FakeAsyncStream([
            interaction_lifecycle_event("interaction.created"),
            interaction_lifecycle_event("interaction.completed"),
        ]),
        "tertiary": lambda: FakeAsyncStream([audio_event(payload)]),
    })
    pipeline._gemini_tts_client = client

    with caplog.at_level(logging.WARNING):
        await pipeline._tts_gemini_sinhala("ඔව්, හරි.")

    calls = client.aio.interactions.calls
    assert [c["model"] for c in calls] == ["secondary", "tertiary"]
    assert transport.audio, "audio must reach the wire via the retried model"
    assert transport.marks == ["tts_done"]
    assert (
        "event=sinhala_tts_model_retry from=secondary to=tertiary "
        "reason=empty_audio" in caplog.text
    )
    assert server._smartpbx_sinhala_tts_model_is_exhausted("secondary") is False
    assert server._smartpbx_sinhala_tts_model_is_exhausted("tertiary") is False
    assert pipeline._smartpbx_tts_model_retries_total == 1


@pytest.mark.asyncio
async def test_invalid_request_on_the_primary_model_still_never_falls_back(monkeypatch):
    """The retry above is scoped to a NON-primary model -- a primary-model
    invalid_request must keep the existing single-call, no-retry contract."""
    import server

    pipeline, _transport = make_sinhala_smartpbx_pipeline(server)
    monkeypatch.setattr(server, "GEMINI_API_KEY", "test-key")
    client = FakeGeminiTTSClient(FakeAsyncStream([error_event("invalid_request_error")]))
    pipeline._gemini_tts_client = client

    await pipeline._tts_gemini_sinhala("ආයුබෝවන්.")

    assert len(client.aio.interactions.calls) == 1
    assert client.aio.interactions.calls[0]["model"] == server.SMARTPBX_SINHALA_GEMINI_TTS_MODEL
    assert pipeline._smartpbx_tts_model_retries_total == 0


@pytest.mark.asyncio
async def test_text_specific_retry_exhausts_the_chain_then_speaks_the_apology(monkeypatch):
    """Every model failing for a text-specific reason must still end in the
    existing never-silent apology, with no model marked exhausted."""
    import server

    monkeypatch.setattr(server, "SMARTPBX_SINHALA_GEMINI_TTS_MODEL", "primary")
    monkeypatch.setattr(
        server, "SMARTPBX_SINHALA_GEMINI_TTS_FALLBACK_MODELS", ("secondary", "tertiary"),
    )
    server._mark_smartpbx_sinhala_tts_model_exhausted("primary")
    pipeline, transport = make_sinhala_smartpbx_pipeline(server)
    monkeypatch.setattr(server, "GEMINI_API_KEY", "test-key")
    server._store_cached_smartpbx_sinhala_phrase_audio(
        server.SMARTPBX_SINHALA_TTS_UNAVAILABLE_TEXT, b"\xff" * 640,
    )
    client = _model_routed_client({
        "secondary": lambda: FakeAsyncStream([
            interaction_lifecycle_event("interaction.created"),
            interaction_lifecycle_event("interaction.completed"),
        ]),
        "tertiary": lambda: FakeAsyncStream([error_event("invalid_request_error")]),
    })
    pipeline._gemini_tts_client = client

    await pipeline._tts_gemini_sinhala("ඔව්, හරි.")

    assert [c["model"] for c in client.aio.interactions.calls] == ["secondary", "tertiary"]
    assert b"".join(transport.audio) == b"\xff" * 640
    assert transport.marks == ["tts_done"]
    assert server._smartpbx_sinhala_tts_model_is_exhausted("secondary") is False
    assert server._smartpbx_sinhala_tts_model_is_exhausted("tertiary") is False
    assert pipeline._smartpbx_tts_model_retries_total == 1


# --- short-utterance cache bank (2026-09-04) --------------------------------

def test_short_utterance_bank_is_small_and_on_the_cache_allowlist():
    import server

    assert 0 < len(server.SMARTPBX_SINHALA_SHORT_UTTERANCE_BANK) <= 10
    for phrase in server.SMARTPBX_SINHALA_SHORT_UTTERANCE_BANK:
        assert phrase in server.SMARTPBX_SINHALA_CACHED_PHRASES


def test_cache_lookup_matches_punctuation_and_whitespace_variance():
    import server

    server._store_cached_smartpbx_sinhala_phrase_audio("ඔව්, හරි.", b"\xff" * 640)

    assert server._is_smartpbx_sinhala_cacheable_phrase("ඔව්, හරි") is True
    assert server._get_cached_smartpbx_sinhala_phrase_audio("ඔව්, හරි") == b"\xff" * 640
    assert server._get_cached_smartpbx_sinhala_phrase_audio("  ඔව්,   හරි.  ") == b"\xff" * 640
    assert server._get_cached_smartpbx_sinhala_phrase_audio("something else") is None


@pytest.mark.asyncio
async def test_short_utterance_bank_phrase_variant_served_from_cache_without_a_live_request(
    monkeypatch,
):
    import server

    pipeline, transport = make_sinhala_smartpbx_pipeline(server)
    monkeypatch.setattr(server, "GEMINI_API_KEY", "test-key")
    server._store_cached_smartpbx_sinhala_phrase_audio("ඔව්, හරි.", b"\xff" * 640)

    def _forbidden_client():
        raise AssertionError("a cached short utterance must not touch the live API")

    monkeypatch.setattr(server, "_get_gemini_tts_client", _forbidden_client)

    await pipeline._tts_gemini_sinhala("ඔව්,  හරි")

    assert b"".join(transport.audio) == b"\xff" * 640
    assert transport.marks == ["tts_done"]


# --- non-streaming model config (2026-09-04) --------------------------------

def test_parse_smartpbx_sinhala_tts_non_streaming_models_env_validation():
    import server

    default = frozenset({"gemini-2.5-flash-preview-tts", "gemini-2.5-pro-preview-tts"})
    assert server._parse_smartpbx_sinhala_tts_non_streaming_models("") == default
    assert server._parse_smartpbx_sinhala_tts_non_streaming_models("   ") == default
    assert server._parse_smartpbx_sinhala_tts_non_streaming_models("model-a,model-b") == (
        frozenset({"model-a", "model-b"})
    )
    assert server._parse_smartpbx_sinhala_tts_non_streaming_models("Bad_Model,model-ok,,") == (
        frozenset({"model-ok"})
    )
    assert server._parse_smartpbx_sinhala_tts_non_streaming_models("INVALID_ONLY") == default


def test_smartpbx_sinhala_tts_min_chars_default():
    import server

    assert server.SMARTPBX_SINHALA_TTS_MIN_CHARS == 12


def test_smartpbx_sinhala_tts_model_is_non_streaming(monkeypatch):
    import server

    monkeypatch.setattr(
        server, "SMARTPBX_SINHALA_TTS_NON_STREAMING_MODELS", frozenset({"fallback-a"}),
    )
    assert server._smartpbx_sinhala_tts_model_is_non_streaming("fallback-a") is True
    assert server._smartpbx_sinhala_tts_model_is_non_streaming("primary") is False


def test_smartpbx_sinhala_tts_current_model_prefers_available_head_over_active(monkeypatch):
    import server

    monkeypatch.setattr(server, "SMARTPBX_SINHALA_GEMINI_TTS_MODEL", "primary")
    monkeypatch.setattr(server, "SMARTPBX_SINHALA_GEMINI_TTS_FALLBACK_MODELS", ("secondary",))

    assert server._smartpbx_sinhala_tts_current_model() == "primary"

    server._mark_smartpbx_sinhala_tts_model_exhausted("primary")
    assert server._smartpbx_sinhala_tts_current_model() == "secondary"



# --- 2026-09-05 coordinator follow-up: length-agnostic retry + empty-stream
# event-kind histogram --------------------------------------------------

@pytest.mark.asyncio
async def test_empty_audio_retry_applies_regardless_of_text_length(monkeypatch, caplog):
    """The empty_audio/invalid_request retry on a non-primary model is not
    gated on the short-utterance case -- any text length must retry."""
    import server

    monkeypatch.setattr(server, "SMARTPBX_SINHALA_GEMINI_TTS_MODEL", "primary")
    monkeypatch.setattr(
        server, "SMARTPBX_SINHALA_GEMINI_TTS_FALLBACK_MODELS", ("secondary", "tertiary"),
    )
    server._mark_smartpbx_sinhala_tts_model_exhausted("primary")
    pipeline, transport = make_sinhala_smartpbx_pipeline(server)
    monkeypatch.setattr(server, "GEMINI_API_KEY", "test-key")
    payload = bytes(range(256)) * 4
    long_text = (
        "අද අපගේ පහසුකම් සියල්ල විවෘතව පවතී. ඔබට කැමති කාමරයක් වෙන් කරගැනීමට "
        "දැන් අවස්ථාව තිබේ."
    )
    assert len(long_text) > server.SMARTPBX_SINHALA_TTS_MIN_CHARS
    client = _model_routed_client({
        "secondary": lambda: FakeAsyncStream([
            interaction_lifecycle_event("interaction.created"),
            interaction_lifecycle_event("interaction.completed"),
        ]),
        "tertiary": lambda: FakeAsyncStream([audio_event(payload)]),
    })
    pipeline._gemini_tts_client = client

    with caplog.at_level(logging.WARNING):
        await pipeline._tts_gemini_sinhala(long_text)

    assert [c["model"] for c in client.aio.interactions.calls] == ["secondary", "tertiary"]
    assert transport.audio, "a long text must retry to the next model exactly like a short one"
    assert (
        "event=sinhala_tts_model_retry from=secondary to=tertiary "
        "reason=empty_audio" in caplog.text
    )
    assert server._smartpbx_sinhala_tts_model_is_exhausted("secondary") is False


@pytest.mark.asyncio
async def test_empty_stream_retry_logs_a_bounded_event_kind_histogram(monkeypatch, caplog):
    """An empty-audio retry also logs a bounded, privacy-safe histogram of
    the event kinds the failing attempt actually saw."""
    import server

    monkeypatch.setattr(server, "SMARTPBX_SINHALA_GEMINI_TTS_MODEL", "primary")
    monkeypatch.setattr(
        server, "SMARTPBX_SINHALA_GEMINI_TTS_FALLBACK_MODELS", ("secondary", "tertiary"),
    )
    server._mark_smartpbx_sinhala_tts_model_exhausted("primary")
    pipeline, _transport = make_sinhala_smartpbx_pipeline(server)
    monkeypatch.setattr(server, "GEMINI_API_KEY", "test-key")
    payload = bytes(range(256)) * 4
    client = _model_routed_client({
        "secondary": lambda: FakeAsyncStream([
            interaction_lifecycle_event("interaction.created"),
            interaction_lifecycle_event("interaction.created"),
            interaction_lifecycle_event("interaction.completed"),
        ]),
        "tertiary": lambda: FakeAsyncStream([audio_event(payload)]),
    })
    pipeline._gemini_tts_client = client

    with caplog.at_level(logging.WARNING):
        await pipeline._tts_gemini_sinhala("ඔව්, හරි.")

    assert (
        "event=sinhala_tts_stream_empty model=secondary "
        "events=interaction.created=2 interaction.completed=1" in caplog.text
    )
    # Never the text, never anything beyond the bounded SDK-protocol tags.
    assert "ඔව්, හරි" not in caplog.text


@pytest.mark.asyncio
async def test_terminal_empty_audio_apology_also_logs_the_event_kind_histogram(
    monkeypatch, caplog,
):
    """The final give-up (every model exhausted for this text) still logs the
    histogram for the last attempt, not just an intermediate retry."""
    import server

    pipeline, transport = make_sinhala_smartpbx_pipeline(server)
    monkeypatch.setattr(server, "GEMINI_API_KEY", "test-key")
    server._store_cached_smartpbx_sinhala_phrase_audio(
        server.SMARTPBX_SINHALA_TTS_UNAVAILABLE_TEXT, b"\xff" * 640,
    )
    pipeline._gemini_tts_client = FakeGeminiTTSClient(FakeAsyncStream([
        interaction_lifecycle_event("interaction.created"),
        interaction_lifecycle_event("interaction.completed"),
    ]))

    with caplog.at_level(logging.WARNING):
        await pipeline._tts_gemini_sinhala("private Sinhala text")

    assert b"".join(transport.audio) == b"\xff" * 640
    assert (
        "event=sinhala_tts_stream_empty model=" in caplog.text
        and "events=interaction.created=1 interaction.completed=1" in caplog.text
    )
    assert "private Sinhala text" not in caplog.text


def test_event_kind_histogram_is_bounded_and_folds_overflow_into_other():
    import server

    max_kinds = server._GEMINI_TTS_EVENT_KIND_HISTOGRAM_MAX_KINDS
    counts: dict[str, int] = {}
    for i in range(max_kinds + 4):
        server._note_gemini_tts_event_kind(counts, f"kind-{i}")
    # max_kinds distinct real kinds, plus one "other" bucket for the overflow.
    assert len(counts) == max_kinds + 1
    assert counts["other"] == 4
    assert all(counts[f"kind-{i}"] == 1 for i in range(max_kinds))
    assert server._format_gemini_tts_event_kind_histogram({"a": 2, "b": 1}) == "a=2 b=1"


def test_note_gemini_tts_event_kind_is_a_noop_without_a_counts_dict():
    import server

    # Must never raise when the caller passes no histogram (e.g. the
    # short-lived prewarm synthesis path, which has no failure telemetry).
    server._note_gemini_tts_event_kind(None, "step.delta")
