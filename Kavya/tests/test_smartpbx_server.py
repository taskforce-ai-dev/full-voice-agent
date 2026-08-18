"""Kavya SmartPBX media-session and service-mode contract tests."""

import asyncio
import json
import logging
from types import SimpleNamespace

import pytest

from smartpbx_protocol import CallContext, MediaFormat
from smartpbx_session import KavyaSmartPBXSession


class FakeTransport:
    def __init__(self):
        self.audio = []
        self.clears = 0
        self.marks = []

    async def send_audio(self, audio):
        self.audio.append(audio)

    async def clear_audio(self):
        self.clears += 1

    async def send_mark(self, name):
        self.marks.append(name)


class GenerationTrackingTransport(FakeTransport):
    """Production-shaped clear fence for delivery-generation regressions."""

    def __init__(self):
        super().__init__()
        self.generation = 0

    async def clear_audio(self):
        await super().clear_audio()
        self.generation += 1


class PauseFirstClearGenerationTransport(GenerationTrackingTransport):
    """Let a newer owner clear while an older owner's clear is suspended."""

    def __init__(self):
        super().__init__()
        self.first_clear_entered = asyncio.Event()
        self.resume_first_clear = asyncio.Event()

    async def clear_audio(self):
        self.clears += 1
        if self.clears == 1:
            self.first_clear_entered.set()
            await self.resume_first_clear.wait()
        self.generation += 1


class BlockingMarkTransport(FakeTransport):
    def __init__(self):
        super().__init__()
        self.mark_entered = asyncio.Event()
        self.release_mark = asyncio.Event()

    async def send_mark(self, name):
        self.marks.append(name)
        self.mark_entered.set()
        await self.release_mark.wait()


class FakeSTT:
    def __init__(self):
        self.starts = 0
        self.audio = []
        self.stops = 0

    def start(self):
        self.starts += 1

    def feed(self, audio):
        self.audio.append(audio)

    def stop(self):
        self.stops += 1


class FakePipeline:
    def __init__(self):
        self.lang = "poison-lang"
        self.call_sid = "poison-call"
        self.caller_phone = "poison-caller"
        self.call_start_time = "poison-time"
        self.full_transcript = [{"role": "user", "text": "I need a room."}]
        self.anthropic_client = object()
        self.client = object()
        self.gemini_client = object()
        self._event_loop = None
        self._stt = None
        self._endpointing_handle = None
        self._audio_dump = []
        self._reprompt_task = None
        self.cancel_reprompt_calls = 0
        self.write_audio_dump_calls = 0
        self.spoken = []

    def _on_stt_result(self, _text):
        pass

    def _on_stt_interim(self, _text):
        pass

    def _cancel_reprompt(self):
        self.cancel_reprompt_calls += 1

    def _write_audio_dump(self):
        self.write_audio_dump_calls += 1

    async def _speak(self, text):
        self.spoken.append(text)


class CapturingTTSResponse:
    status_code = 200

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc_value, traceback):
        return False

    async def aread(self):
        return b""

    async def aiter_bytes(self, chunk_size):
        assert chunk_size == 640
        yield b"ulaw-frame"


class CapturingTTSClient:
    def __init__(self):
        self.requests = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc_value, traceback):
        return False

    def stream(self, method, url, *, json, headers, timeout):
        self.requests.append({"method": method, "url": url, "json": json, "headers": headers, "timeout": timeout})
        return CapturingTTSResponse()


class FailingTTSResponse(CapturingTTSResponse):
    def __init__(self, status_code, body=b""):
        self.status_code = status_code
        self.body = body

    async def aread(self):
        return self.body


class FailingTTSClient(CapturingTTSClient):
    def __init__(self, response):
        super().__init__()
        self.response = response

    def stream(self, method, url, *, json, headers, timeout):
        self.requests.append({"method": method, "url": url, "json": json, "headers": headers, "timeout": timeout})
        return self.response


class RaisingTTSResponse:
    def __init__(self, exception):
        self.exception = exception

    async def __aenter__(self):
        raise self.exception

    async def __aexit__(self, exc_type, exc_value, traceback):
        return False


def smartpbx_tts_pipeline(server, sink):
    pipeline = server.MediaStreamSession(websocket=None, lang="en", media_transport=FakeTransport())
    pipeline._smartpbx_transfer_context = object()

    def record(stage, outcome, failure_class):
        sink((stage, outcome, failure_class))

    pipeline._smartpbx_diagnostic_sink = record
    return pipeline


def assert_no_tts_secret_leakage(caplog, *secrets):
    for secret in secrets:
        assert secret not in caplog.text


def raising_tts_sink(records, exception):
    def sink(event):
        records.append(event)
        raise RuntimeError(exception)

    return sink


@pytest.mark.asyncio
async def test_smartpbx_english_tts_uses_profile_without_general_voice(monkeypatch):
    import server
    from english_voice_profile import load_kavya_english_voice_profile

    client = CapturingTTSClient()
    profile = load_kavya_english_voice_profile({"KAVYA_EN_ELEVENLABS_VOICE_ID": "unit-test-canonical-voice"})
    monkeypatch.setattr(server, "ELEVENLABS_API_KEY", "unit-test-api-key")
    monkeypatch.setattr(server, "ELEVENLABS_VOICE_ID", "")
    monkeypatch.setattr(server, "load_kavya_english_voice_profile", lambda: profile)
    monkeypatch.setattr(server.httpx, "AsyncClient", lambda: client)
    pipeline = server.MediaStreamSession(websocket=None, lang="en", media_transport=FakeTransport())
    await pipeline._tts_elevenlabs("Hello from Kavya.")
    request = client.requests[0]
    assert request["url"] == (
        "https://api.elevenlabs.io/v1/text-to-speech/unit-test-canonical-voice/stream"
        "?output_format=ulaw_8000&optimize_streaming_latency=3"
    )
    assert request["json"] == {"text": "Hello from Kavya.", "model_id": "eleven_flash_v2_5", "voice_settings": {"stability": 0.5, "similarity_boost": 0.75, "style": 0.0, "use_speaker_boost": True}}
    assert "output_format" not in request["json"]
    assert "mp3" not in request["url"]


@pytest.mark.asyncio
async def test_smartpbx_english_tts_fails_closed_when_profile_is_unavailable(monkeypatch):
    import server

    client = CapturingTTSClient()
    monkeypatch.setattr(server, "ELEVENLABS_API_KEY", "unit-test-api-key")
    monkeypatch.setattr(server, "ELEVENLABS_VOICE_ID", "unit-test-general-voice")
    monkeypatch.setattr(server, "load_kavya_english_voice_profile", lambda: (_ for _ in ()).throw(ValueError("KAVYA_EN_ELEVENLABS_VOICE_ID must be configured")))
    monkeypatch.setattr(server.httpx, "AsyncClient", lambda: client)
    pipeline = server.MediaStreamSession(websocket=None, lang="en", media_transport=FakeTransport())
    await pipeline._tts_elevenlabs("Hello from Kavya.")
    assert client.requests == []


@pytest.mark.asyncio
async def test_retained_non_english_tts_still_requires_general_voice(monkeypatch):
    import server

    client = CapturingTTSClient()
    monkeypatch.setattr(server, "ELEVENLABS_API_KEY", "unit-test-api-key")
    monkeypatch.setattr(server, "ELEVENLABS_VOICE_ID", "")
    monkeypatch.setattr(server.httpx, "AsyncClient", lambda: client)
    pipeline = server.MediaStreamSession(websocket=None, lang="ta", media_transport=FakeTransport())
    await pipeline._tts_elevenlabs("vanakkam")
    assert client.requests == []


@pytest.mark.asyncio
async def test_smartpbx_english_tts_missing_api_key_emits_finite_diagnostic(monkeypatch, caplog):
    import server
    from smartpbx_diagnostics import DiagnosticFailureClass, DiagnosticOutcome, DiagnosticStage

    records = []
    api_key = "api-key-secret"
    text = "spoken-text-secret"
    monkeypatch.setattr(server, "ELEVENLABS_API_KEY", "")
    pipeline = smartpbx_tts_pipeline(server, records.append)

    with caplog.at_level(logging.WARNING):
        await pipeline._tts_elevenlabs(text)

    assert records == [(DiagnosticStage.TTS, DiagnosticOutcome.FAILED, DiagnosticFailureClass.TTS_MISSING_API_KEY)]
    assert_no_tts_secret_leakage(caplog, api_key, text)


@pytest.mark.asyncio
async def test_smartpbx_english_tts_profile_failure_emits_finite_diagnostic(monkeypatch, caplog):
    import server
    from smartpbx_diagnostics import DiagnosticFailureClass, DiagnosticOutcome, DiagnosticStage

    records = []
    api_key = "api-key-secret"
    voice = "voice-secret"
    exception = "profile-exception-secret"
    text = "spoken-text-secret"
    monkeypatch.setattr(server, "ELEVENLABS_API_KEY", api_key)
    monkeypatch.setattr(
        server,
        "load_kavya_english_voice_profile",
        lambda: (_ for _ in ()).throw(ValueError(f"{voice} {exception}")),
    )
    pipeline = smartpbx_tts_pipeline(server, records.append)

    with caplog.at_level(logging.WARNING):
        await pipeline._tts_elevenlabs(text)

    assert records == [(DiagnosticStage.TTS, DiagnosticOutcome.FAILED, DiagnosticFailureClass.TTS_PROFILE_FAILURE)]
    assert_no_tts_secret_leakage(caplog, api_key, voice, exception, text)


@pytest.mark.asyncio
async def test_smartpbx_english_tts_http_failure_emits_finite_diagnostic(monkeypatch, caplog):
    import server
    from smartpbx_diagnostics import DiagnosticFailureClass, DiagnosticOutcome, DiagnosticStage

    records = []
    api_key = "api-key-secret"
    voice = "voice-secret"
    body = "response-body-secret"
    text = "spoken-text-secret"
    profile = SimpleNamespace(voice_id=voice, model_id="test-model", request_voice_settings={})
    client = FailingTTSClient(FailingTTSResponse(599, body.encode()))
    monkeypatch.setattr(server, "ELEVENLABS_API_KEY", api_key)
    monkeypatch.setattr(server, "load_kavya_english_voice_profile", lambda: profile)
    monkeypatch.setattr(server.httpx, "AsyncClient", lambda: client)
    pipeline = smartpbx_tts_pipeline(server, records.append)

    with caplog.at_level(logging.ERROR):
        await pipeline._tts_elevenlabs(text)

    assert records == [(DiagnosticStage.TTS, DiagnosticOutcome.FAILED, DiagnosticFailureClass.TTS_HTTP_STATUS)]
    assert_no_tts_secret_leakage(caplog, api_key, voice, body, text)


@pytest.mark.asyncio
async def test_smartpbx_english_tts_timeout_emits_finite_diagnostic(monkeypatch, caplog):
    import server
    from smartpbx_diagnostics import DiagnosticFailureClass, DiagnosticOutcome, DiagnosticStage

    records = []
    api_key = "api-key-secret"
    voice = "voice-secret"
    exception = "timeout-exception-secret"
    text = "spoken-text-secret"
    profile = SimpleNamespace(voice_id=voice, model_id="test-model", request_voice_settings={})
    client = FailingTTSClient(RaisingTTSResponse(server.httpx.TimeoutException(exception)))
    monkeypatch.setattr(server, "ELEVENLABS_API_KEY", api_key)
    monkeypatch.setattr(server, "load_kavya_english_voice_profile", lambda: profile)
    monkeypatch.setattr(server.httpx, "AsyncClient", lambda: client)
    pipeline = smartpbx_tts_pipeline(server, records.append)

    with caplog.at_level(logging.ERROR):
        await pipeline._tts_elevenlabs(text)

    assert records == [(DiagnosticStage.TTS, DiagnosticOutcome.FAILED, DiagnosticFailureClass.TTS_TIMEOUT)]
    assert_no_tts_secret_leakage(caplog, api_key, voice, exception, text)


@pytest.mark.asyncio
async def test_smartpbx_english_tts_exception_emits_finite_diagnostic(monkeypatch, caplog):
    import server
    from smartpbx_diagnostics import DiagnosticFailureClass, DiagnosticOutcome, DiagnosticStage

    records = []
    api_key = "api-key-secret"
    voice = "voice-secret"
    exception = "generic-exception-secret"
    text = "spoken-text-secret"
    profile = SimpleNamespace(voice_id=voice, model_id="test-model", request_voice_settings={})
    client = FailingTTSClient(RaisingTTSResponse(RuntimeError(exception)))
    monkeypatch.setattr(server, "ELEVENLABS_API_KEY", api_key)
    monkeypatch.setattr(server, "load_kavya_english_voice_profile", lambda: profile)
    monkeypatch.setattr(server.httpx, "AsyncClient", lambda: client)
    pipeline = smartpbx_tts_pipeline(server, records.append)

    with caplog.at_level(logging.ERROR):
        await pipeline._tts_elevenlabs(text)

    assert records == [(DiagnosticStage.TTS, DiagnosticOutcome.FAILED, DiagnosticFailureClass.TTS_EXCEPTION)]
    assert_no_tts_secret_leakage(caplog, api_key, voice, exception, text)


@pytest.mark.asyncio
async def test_smartpbx_english_tts_missing_api_key_isolates_raising_diagnostic_sink(monkeypatch, caplog):
    import server
    from smartpbx_diagnostics import DiagnosticFailureClass, DiagnosticOutcome, DiagnosticStage

    records = []
    callback_exception = "diagnostic-sink-exception-secret"
    text = "spoken-text-secret"
    monkeypatch.setattr(server, "ELEVENLABS_API_KEY", "")
    pipeline = smartpbx_tts_pipeline(server, raising_tts_sink(records, callback_exception))

    with caplog.at_level(logging.WARNING):
        result = await pipeline._tts_elevenlabs(text)

    assert result is None
    assert pipeline._is_speaking is False
    assert records == [(DiagnosticStage.TTS, DiagnosticOutcome.FAILED, DiagnosticFailureClass.TTS_MISSING_API_KEY)]
    assert_no_tts_secret_leakage(caplog, callback_exception, text)


@pytest.mark.asyncio
async def test_smartpbx_english_tts_profile_failure_isolates_raising_diagnostic_sink(monkeypatch, caplog):
    import server
    from smartpbx_diagnostics import DiagnosticFailureClass, DiagnosticOutcome, DiagnosticStage

    records = []
    api_key = "api-key-secret"
    voice = "voice-secret"
    profile_exception = "profile-exception-secret"
    callback_exception = "diagnostic-sink-exception-secret"
    text = "spoken-text-secret"
    monkeypatch.setattr(server, "ELEVENLABS_API_KEY", api_key)
    monkeypatch.setattr(
        server,
        "load_kavya_english_voice_profile",
        lambda: (_ for _ in ()).throw(ValueError(f"{voice} {profile_exception}")),
    )
    pipeline = smartpbx_tts_pipeline(server, raising_tts_sink(records, callback_exception))

    with caplog.at_level(logging.WARNING):
        result = await pipeline._tts_elevenlabs(text)

    assert result is None
    assert pipeline._is_speaking is False
    assert records == [(DiagnosticStage.TTS, DiagnosticOutcome.FAILED, DiagnosticFailureClass.TTS_PROFILE_FAILURE)]
    assert_no_tts_secret_leakage(caplog, api_key, voice, profile_exception, callback_exception, text)


@pytest.mark.asyncio
async def test_smartpbx_english_tts_http_failure_isolates_raising_diagnostic_sink(monkeypatch, caplog):
    import server
    from smartpbx_diagnostics import DiagnosticFailureClass, DiagnosticOutcome, DiagnosticStage

    records = []
    api_key = "api-key-secret"
    voice = "voice-secret"
    body = "response-body-secret"
    callback_exception = "diagnostic-sink-exception-secret"
    text = "spoken-text-secret"
    profile = SimpleNamespace(voice_id=voice, model_id="test-model", request_voice_settings={})
    client = FailingTTSClient(FailingTTSResponse(599, body.encode()))
    monkeypatch.setattr(server, "ELEVENLABS_API_KEY", api_key)
    monkeypatch.setattr(server, "load_kavya_english_voice_profile", lambda: profile)
    monkeypatch.setattr(server.httpx, "AsyncClient", lambda: client)
    pipeline = smartpbx_tts_pipeline(server, raising_tts_sink(records, callback_exception))

    with caplog.at_level(logging.ERROR):
        result = await pipeline._tts_elevenlabs(text)

    assert result is None
    assert pipeline._is_speaking is False
    assert records == [(DiagnosticStage.TTS, DiagnosticOutcome.FAILED, DiagnosticFailureClass.TTS_HTTP_STATUS)]
    assert_no_tts_secret_leakage(caplog, api_key, voice, body, callback_exception, text)


@pytest.mark.asyncio
async def test_smartpbx_english_tts_timeout_isolates_raising_diagnostic_sink(monkeypatch, caplog):
    import server
    from smartpbx_diagnostics import DiagnosticFailureClass, DiagnosticOutcome, DiagnosticStage

    records = []
    api_key = "api-key-secret"
    voice = "voice-secret"
    timeout_exception = "timeout-exception-secret"
    callback_exception = "diagnostic-sink-exception-secret"
    text = "spoken-text-secret"
    profile = SimpleNamespace(voice_id=voice, model_id="test-model", request_voice_settings={})
    client = FailingTTSClient(RaisingTTSResponse(server.httpx.TimeoutException(timeout_exception)))
    monkeypatch.setattr(server, "ELEVENLABS_API_KEY", api_key)
    monkeypatch.setattr(server, "load_kavya_english_voice_profile", lambda: profile)
    monkeypatch.setattr(server.httpx, "AsyncClient", lambda: client)
    pipeline = smartpbx_tts_pipeline(server, raising_tts_sink(records, callback_exception))

    with caplog.at_level(logging.ERROR):
        result = await pipeline._tts_elevenlabs(text)

    assert result is None
    assert pipeline._is_speaking is False
    assert records == [(DiagnosticStage.TTS, DiagnosticOutcome.FAILED, DiagnosticFailureClass.TTS_TIMEOUT)]
    assert_no_tts_secret_leakage(caplog, api_key, voice, timeout_exception, callback_exception, text)


@pytest.mark.asyncio
async def test_smartpbx_english_tts_exception_isolates_raising_diagnostic_sink(monkeypatch, caplog):
    import server
    from smartpbx_diagnostics import DiagnosticFailureClass, DiagnosticOutcome, DiagnosticStage

    records = []
    api_key = "api-key-secret"
    voice = "voice-secret"
    tts_exception = "generic-exception-secret"
    callback_exception = "diagnostic-sink-exception-secret"
    text = "spoken-text-secret"
    profile = SimpleNamespace(voice_id=voice, model_id="test-model", request_voice_settings={})
    client = FailingTTSClient(RaisingTTSResponse(RuntimeError(tts_exception)))
    monkeypatch.setattr(server, "ELEVENLABS_API_KEY", api_key)
    monkeypatch.setattr(server, "load_kavya_english_voice_profile", lambda: profile)
    monkeypatch.setattr(server.httpx, "AsyncClient", lambda: client)
    pipeline = smartpbx_tts_pipeline(server, raising_tts_sink(records, callback_exception))

    with caplog.at_level(logging.ERROR):
        result = await pipeline._tts_elevenlabs(text)

    assert result is None
    assert pipeline._is_speaking is False
    assert records == [(DiagnosticStage.TTS, DiagnosticOutcome.FAILED, DiagnosticFailureClass.TTS_EXCEPTION)]
    assert_no_tts_secret_leakage(caplog, api_key, voice, tts_exception, callback_exception, text)


@pytest.mark.asyncio
async def test_smartpbx_english_tts_without_sink_keeps_existing_failure_behavior(monkeypatch):
    import server

    pipeline = server.MediaStreamSession(websocket=None, lang="en", media_transport=FakeTransport())
    pipeline._smartpbx_transfer_context = object()
    monkeypatch.setattr(server, "ELEVENLABS_API_KEY", "")

    await pipeline._tts_elevenlabs("spoken-text-secret")

    assert pipeline._is_speaking is False


@pytest.mark.asyncio
async def test_non_smartpbx_tts_failure_does_not_emit_smartpbx_diagnostic(monkeypatch):
    import server

    records = []
    pipeline = server.MediaStreamSession(websocket=None, lang="en", media_transport=FakeTransport())
    pipeline._smartpbx_diagnostic_sink = records.append
    monkeypatch.setattr(server, "ELEVENLABS_API_KEY", "")

    await pipeline._tts_elevenlabs("spoken-text-secret")

    assert records == []


def context(**overrides):
    values = {
        "call_id": "dialog-media-leg",
        "other_leg_call_id": "dialog-safe-call",
        "caller_id_number": "+94000000000",
        "callee_id_number": "+94110000000",
        "account_id": "dialog-account",
        "media_format": MediaFormat(encoding="g711_ulaw", sample_rate=8000),
    }
    values.update(overrides)
    return CallContext(**values)


def make_session(*, post_call_processor, pipeline=None, stt=None, transport=None):
    pipeline = pipeline or FakePipeline()
    stt = stt or FakeSTT()
    transport = transport or FakeTransport()
    session = KavyaSmartPBXSession(
        context(),
        transport,
        pipeline=pipeline,
        stt_factory=lambda **_kwargs: stt,
        post_call_processor=post_call_processor,
        welcome_text="Welcome to Hatton Hills.",
        llm_provider="claude",
        model="test-model",
    )
    return session, pipeline, stt, transport


@pytest.mark.asyncio
async def test_default_smartpbx_welcome_reuses_kavya_english_greeting():
    import server

    async def process_post_call(**_metadata):
        pass

    pipeline = FakePipeline()
    session = KavyaSmartPBXSession(
        context(),
        FakeTransport(),
        pipeline=pipeline,
        stt_factory=lambda **_kwargs: FakeSTT(),
        post_call_processor=process_post_call,
        welcome_text=None,
        llm_provider="claude",
        model="test-model",
    )

    await session.start()
    await asyncio.sleep(0)
    await session.finish(False)

    assert pipeline.spoken == [server.LANGUAGE_CONFIGS["en"]["welcome_greeting"]]


@pytest.mark.asyncio
async def test_dialog_finish_cancels_pending_initial_filler_before_other_teardown_awaits():
    """Item 2 (PR-266 review round 2): KavyaSmartPBXSession._finish_once must
    cancel-and-await the active initial-round filler BEFORE any other
    teardown await, so a 1.5s (here, shortened) filler timer can't fire into
    a session that is already tearing down.

    Without the fix, ``_finish_once`` never touches
    ``pipeline._smartpbx_initial_filler`` at all -- the filler task is left
    running, and once its delay elapses it speaks straight into (and queues
    transport audio for) an already-finished session.
    """
    import server

    async def process_post_call(**_metadata):
        pass

    pipeline = FakePipeline()
    spoken_generations = []

    async def speak(text, *, generation):
        spoken_generations.append((text, generation))

    # Real controller (not a fake) so this exercises the actual cancel/await
    # machinery, with a short delay so the test doesn't wait out the real
    # 1.5s default.
    controller = server.SmartPBXInitialFillerController(
        speak=speak, generation=0, delay_seconds=0.05, clear_audio=None,
    )
    controller.start()
    pipeline._smartpbx_initial_filler = controller
    # FakePipeline has no _finish_initial_smartpbx_filler of its own -- bind
    # the real MediaStreamSession implementation to it (it only touches
    # self._smartpbx_initial_filler, which duck-types fine here).
    pipeline._finish_initial_smartpbx_filler = (
        lambda ctl: server.MediaStreamSession._finish_initial_smartpbx_filler(pipeline, ctl)
    )

    session = KavyaSmartPBXSession(
        context(),
        FakeTransport(),
        pipeline=pipeline,
        stt_factory=lambda **_kwargs: FakeSTT(),
        post_call_processor=process_post_call,
        welcome_text=None,
        llm_provider="claude",
        model="test-model",
    )
    await session.start()

    # Finish well before the filler's 0.05s delay would elapse.
    task_at_finish_start = controller._task
    await session.finish(False)

    # The filler task must already be finished (cancelled-and-awaited) by
    # the time finish() returns -- not merely requested to cancel.
    assert task_at_finish_start is not None
    assert task_at_finish_start.done()
    assert pipeline._smartpbx_initial_filler is None

    # Wait past the filler's would-be delay to prove it never fires late.
    await asyncio.sleep(0.15)
    assert spoken_generations == []


@pytest.mark.asyncio
async def test_dialog_hangup_finishes_once_and_schedules_kavya_post_call():
    post_call_calls = []

    async def process_post_call(**metadata):
        post_call_calls.append(metadata)

    session, pipeline, stt, _ = make_session(post_call_processor=process_post_call)
    await session.start()
    await asyncio.gather(session.finish(True), session.finish(True))
    await asyncio.sleep(0)

    assert stt.starts == 1
    assert stt.stops == 1
    assert pipeline.cancel_reprompt_calls == 1
    assert len(post_call_calls) == 1
    assert session.terminal_future.done()
    assert session.terminal_future.result() is None


@pytest.mark.asyncio
async def test_dialog_audio_and_post_call_metadata_come_from_validated_context(caplog):
    post_call_calls = []

    async def process_post_call(**metadata):
        post_call_calls.append(metadata)

    session, pipeline, stt, _ = make_session(post_call_processor=process_post_call)
    with caplog.at_level(logging.INFO):
        await session.start()
        await session.feed_audio(b"\x01\x02")
        await session.finish(True)
        await asyncio.sleep(0)

    assert stt.audio == [b"\x01\x02"]
    assert pipeline.call_sid == "dialog-safe-call"
    assert pipeline.caller_phone == "+94000000000"
    assert len(post_call_calls) == 1
    assert post_call_calls[0]["call_sid"] == "dialog-safe-call"
    assert post_call_calls[0]["caller_phone"] == "+94000000000"
    assert post_call_calls[0]["lang"] == "en"
    assert post_call_calls[0]["privacy_safe"] is True
    assert "dialog-media-leg" not in caplog.text
    assert "dialog-safe-call" not in caplog.text
    assert "+94000000000" not in caplog.text


@pytest.mark.asyncio
async def test_media_pipeline_routes_dialog_output_without_twilio_wire_events():
    import server

    class ForbiddenWebSocket:
        async def send_text(self, _message):
            raise AssertionError("Dialog output must not use Twilio WebSocket JSON")

    transport = FakeTransport()
    pipeline = server.MediaStreamSession(
        websocket=ForbiddenWebSocket(),
        lang="en",
        media_transport=transport,
    )
    pipeline._is_speaking = True

    await pipeline._send_media_audio(b"audio")
    await pipeline._clear_media_audio()
    await pipeline._send_tts_done()

    assert transport.audio == [b"audio"]
    assert transport.clears == 1
    assert transport.marks == ["tts_done"]
    assert pipeline._is_speaking is False


@pytest.mark.asyncio
async def test_smartpbx_english_pipeline_uses_existing_elevenlabs_tts():
    import server

    transport = FakeTransport()
    pipeline = server.MediaStreamSession(
        websocket=None,
        lang="en",
        media_transport=transport,
    )
    spoken = []

    async def elevenlabs(text):
        spoken.append(text)

    pipeline._tts_elevenlabs = elevenlabs
    await pipeline._speak("Hello from Kavya.")

    assert spoken == ["Hello from Kavya."]


@pytest.mark.asyncio
async def test_dialog_media_logs_never_contain_transcript_agent_text_or_call_id(caplog, monkeypatch):
    import server

    secret_guest = "guest transcript must stay private"
    secret_agent = "agent response must stay private"
    secret_call = "dialog-call-id-must-stay-private"
    pipeline = server.MediaStreamSession(websocket=None, lang="en", media_transport=FakeTransport())
    pipeline._smartpbx_transfer_context = object()
    pipeline.call_sid = secret_call
    pipeline._pending_transcript = secret_guest
    pipeline._event_loop = asyncio.get_running_loop()

    monkeypatch.setattr(server, "retrieve_context", lambda _text: "")

    async def response():
        return secret_agent

    async def no_speak(_text, generation=-1):
        return None

    monkeypatch.setattr(pipeline, "_run_llm_claude", response)
    monkeypatch.setattr(pipeline, "_speak", no_speak)
    with caplog.at_level(logging.INFO):
        await pipeline._flush_transcript()

    assert secret_guest not in caplog.text
    assert secret_agent not in caplog.text
    assert secret_call not in caplog.text
    assert "smartpbx_media event=guest_utterance" in caplog.text
    assert "smartpbx_media event=agent_response" in caplog.text


@pytest.mark.asyncio
async def test_legacy_media_logs_keep_existing_transcript_and_call_id_semantics(caplog):
    import server

    pipeline = server.MediaStreamSession(websocket=None, lang="en", media_transport=FakeTransport())
    pipeline.call_sid = "legacy-call-id"
    pipeline._pending_transcript = "legacy transcript"
    pipeline._event_loop = asyncio.get_running_loop()

    async def no_process(_text):
        return None

    pipeline._process_utterance = no_process
    with caplog.at_level(logging.INFO):
        await pipeline._flush_transcript()

    assert "legacy-call-id" in caplog.text
    assert "legacy transcript" in caplog.text


def test_smartpbx_stt_streams_can_disable_raw_sdk_transcript_logging(caplog):
    import server

    class Event:
        class Result:
            text = "raw azure stt transcript"
        result = Result()

    stream = server.AzureSTTStream(lambda _text: None, lambda _text: None, "en", privacy_safe=True)
    with caplog.at_level(logging.INFO):
        stream._on_recognizing(Event())

    assert "raw azure stt transcript" not in caplog.text


def test_google_smartpbx_english_stt_constructs_en_us_without_duplicate_alternative(monkeypatch):
    import server

    captured = {}

    class RecognitionConfig:
        AudioEncoding = SimpleNamespace(MULAW="mulaw")

        def __init__(self, **kwargs):
            self.kwargs = kwargs

    class StreamingRecognitionConfig:
        def __init__(self, **kwargs):
            self.config = kwargs["config"]

    class SpeechClient:
        def streaming_recognize(self, *, config, requests):
            captured["config"] = config.config.kwargs
            return []

    fake_google = SimpleNamespace(
        SpeechClient=SpeechClient,
        RecognitionConfig=RecognitionConfig,
        StreamingRecognitionConfig=StreamingRecognitionConfig,
        StreamingRecognizeRequest=lambda **kwargs: kwargs,
    )
    monkeypatch.setattr(server, "google_speech", fake_google)
    stream = server.GoogleSTTStream(lambda _text: None, lang="en", privacy_safe=True)
    stream._running = True

    stream._run_one_stream()

    assert captured["config"]["language_code"] == "en-US"
    assert "en-US" not in captured["config"]["alternative_language_codes"]


def test_azure_smartpbx_english_stt_constructs_en_us(monkeypatch):
    import server

    class Signal:
        def connect(self, _callback):
            return None

    class SpeechConfig:
        def __init__(self, **_kwargs):
            self.speech_recognition_language = None

    class PushAudioInputStream:
        def __init__(self, **_kwargs):
            pass

    class SpeechRecognizer:
        def __init__(self, *, speech_config, audio_config):
            self.speech_config = speech_config
            self.audio_config = audio_config
            self.recognizing = Signal()
            self.recognized = Signal()
            self.canceled = Signal()

        def start_continuous_recognition_async(self):
            return None

    fake_azure = SimpleNamespace(
        SpeechConfig=SpeechConfig,
        SpeechRecognizer=SpeechRecognizer,
        audio=SimpleNamespace(
            AudioStreamFormat=lambda **kwargs: kwargs,
            PushAudioInputStream=PushAudioInputStream,
            AudioConfig=lambda **kwargs: kwargs,
        ),
    )
    monkeypatch.setattr(server, "azure_speech", fake_azure)
    monkeypatch.setattr(server, "AZURE_STT_AVAILABLE", True)
    monkeypatch.setattr(server, "AZURE_SPEECH_KEY", "test-key")
    stream = server.AzureSTTStream(lambda _text: None, lang="en", privacy_safe=True)

    stream.start()

    assert stream._recognizer.speech_config.speech_recognition_language == "en-US"


@pytest.mark.asyncio
async def test_smartpbx_local_tts_completion_schedules_one_silence_nudge():
    import server

    pipeline = server.MediaStreamSession(websocket=None, lang="en", media_transport=FakeTransport())
    pipeline._smartpbx_transfer_context = object()
    blocker = asyncio.Event()
    starts = 0

    async def wait_for_silence():
        nonlocal starts
        starts += 1
        await blocker.wait()

    pipeline._reprompt_after_silence = wait_for_silence
    await pipeline._send_tts_done()
    await asyncio.sleep(0)
    first = pipeline._reprompt_task
    await pipeline._send_tts_done()
    await asyncio.sleep(0)
    second = pipeline._reprompt_task

    assert first is not second
    assert first.cancelled()
    assert starts == 2
    pipeline._cancel_reprompt()
    await asyncio.gather(second, return_exceptions=True)


@pytest.mark.asyncio
async def test_dialog_silence_nudge_log_is_event_only(caplog, monkeypatch):
    import server

    pipeline = server.MediaStreamSession(websocket=None, lang="en", media_transport=FakeTransport())
    pipeline._smartpbx_transfer_context = object()
    pipeline.call_sid = "REPROMPT-CALL-ID-SECRET"
    monkeypatch.setattr(server, "SILENCE_REPROMPT_DELAY", 0)

    async def no_speak(_text, generation=-1):
        return None

    pipeline._speak = no_speak
    with caplog.at_level(logging.INFO):
        await pipeline._reprompt_after_silence()

    assert "REPROMPT-CALL-ID-SECRET" not in caplog.text
    assert "smartpbx_media event=silence_reprompt attempt=1" in caplog.text


@pytest.mark.asyncio
async def test_legacy_tts_done_keeps_wire_mark_without_local_reprompt():
    import server

    class CapturingWebSocket:
        def __init__(self):
            self.messages = []

        async def send_text(self, message):
            self.messages.append(message)

    websocket = CapturingWebSocket()
    pipeline = server.MediaStreamSession(websocket=websocket, lang="si")
    pipeline.stream_sid = "legacy-stream"

    await pipeline._send_tts_done()

    assert '"event": "mark"' in websocket.messages[0]
    assert pipeline._reprompt_task is None


@pytest.mark.asyncio
async def test_finish_cancels_and_awaits_inflight_welcome_speech():
    entered = asyncio.Event()
    cancelled = asyncio.Event()

    class HangingPipeline(FakePipeline):
        async def _speak(self, _text):
            entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()

    async def process_post_call(**_metadata):
        return None

    session, _, _, _ = make_session(
        post_call_processor=process_post_call, pipeline=HangingPipeline()
    )
    await session.start()
    await entered.wait()
    try:
        await session.finish(False)
        assert session._welcome_task.done()
        assert cancelled.is_set()
    finally:
        if not session._welcome_task.done():
            session._welcome_task.cancel()
            await asyncio.gather(session._welcome_task, return_exceptions=True)


def test_dialog_audio_dump_status_never_logs_the_call_id(caplog, monkeypatch, tmp_path):
    import server

    pipeline = server.MediaStreamSession(websocket=None, lang="en", media_transport=FakeTransport())
    pipeline._smartpbx_transfer_context = object()
    pipeline.call_sid = "dialog-audio-dump-call-id"
    pipeline._audio_dump = [b"\xff" * 80]
    monkeypatch.setattr(server, "STT_DEBUG_DIR", str(tmp_path))

    with caplog.at_level(logging.INFO):
        pipeline._write_audio_dump()

    assert "dialog-audio-dump-call-id" not in caplog.text


@pytest.mark.asyncio
async def test_dialog_utterance_context_is_isolated_across_two_sessions_and_resets():
    import server
    from handover import handover_context
    from tools import SmartPBXTransferContext, smartpbx_transfer_context

    first = server.MediaStreamSession(websocket=None, lang="en", media_transport=FakeTransport())
    second = server.MediaStreamSession(websocket=None, lang="en", media_transport=FakeTransport())
    first._smartpbx_transfer_context = SmartPBXTransferContext(None)
    second._smartpbx_transfer_context = SmartPBXTransferContext(None)
    first._smartpbx_caller_context = {"caller_phone": "first-caller"}
    second._smartpbx_caller_context = {"caller_phone": "second-caller"}
    observed = []

    async def capture(label):
        observed.append((label, smartpbx_transfer_context.get(), handover_context.get()))
        await asyncio.sleep(0)

    first._process_utterance_bound = lambda _text: capture("first")
    second._process_utterance_bound = lambda _text: capture("second")
    await asyncio.gather(first._process_utterance("one"), second._process_utterance("two"))

    assert observed == [
        ("first", first._smartpbx_transfer_context, {"caller_phone": "first-caller"}),
        ("second", second._smartpbx_transfer_context, {"caller_phone": "second-caller"}),
    ]
    assert smartpbx_transfer_context.get() is None
    assert handover_context.get() is None


@pytest.mark.asyncio
async def test_dialog_utterance_context_resets_after_failure_and_cancellation():
    import server
    from handover import handover_context
    from tools import SmartPBXTransferContext, smartpbx_transfer_context

    pipeline = server.MediaStreamSession(websocket=None, lang="en", media_transport=FakeTransport())
    pipeline._smartpbx_transfer_context = SmartPBXTransferContext(None)
    pipeline._smartpbx_caller_context = {"caller_phone": "private-caller"}

    async def fail(_text):
        raise RuntimeError("expected")

    pipeline._process_utterance_bound = fail
    with pytest.raises(RuntimeError, match="expected"):
        await pipeline._process_utterance("failure")
    assert smartpbx_transfer_context.get() is None
    assert handover_context.get() is None

    entered = asyncio.Event()

    async def wait_forever(_text):
        entered.set()
        await asyncio.Event().wait()

    pipeline._process_utterance_bound = wait_forever
    task = asyncio.create_task(pipeline._process_utterance("cancellation"))
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert smartpbx_transfer_context.get() is None
    assert handover_context.get() is None



def test_smartpbx_mode_exposes_only_bounded_routes():
    import server

    environment = {
        "ENABLE_SMARTPBX_WSS": "true",
        "SMARTPBX_WS_TOKEN": "do-not-expose",
        "SMARTPBX_ACCOUNT_ID": "do-not-expose-account",
    }
    smartpbx_app = server.build_service_app("smartpbx", environment)
    routes = {route.path: route for route in smartpbx_app.routes}

    assert set(routes) == {
        "/health",
        "/smartpbx/status",
        "/ws/v1/smartpbx/media",
    }
    assert "/voice/incoming" not in routes
    assert "/voice/language-selected" not in routes
    assert "/ws/conversation" not in routes
    assert "/ws/media-stream/{lang}" not in routes

    status = routes["/smartpbx/status"].endpoint(
        _fake_request({"X-Kavya-SmartPBX-Token": "do-not-expose"})
    )
    assert status["enabled"] is True
    assert status["configured"] is True
    assert status["transfer_enabled"] is False
    assert status["active_sessions"] == 0
    assert status["max_sessions"] == 4
    assert "token" not in status
    assert "account_id" not in status
    assert "do-not-expose" not in repr(status)


def test_smartpbx_status_reports_transfer_enabled_only_for_complete_allowlisted_configuration():
    import server

    environment = {
        "ENABLE_SMARTPBX_WSS": "true",
        "SMARTPBX_WS_TOKEN": "test-token",
        "SMARTPBX_ACCOUNT_ID": "account-1",
        "SMARTPBX_MCP_URL": "https://dialog.example:9443/ucp/v2/mcp",
        "SMARTPBX_API_KEY": "api-key-marker",
        "SMARTPBX_MCP_ACCOUNT_HEADER": "account_id",
        "SMARTPBX_TRANSFER_DESTINATIONS_JSON": '{"human_support":"tel:+94110000000"}',
    }

    app = server.build_service_app("smartpbx", environment)
    routes = {route.path: route for route in app.routes}

    assert routes["/smartpbx/status"].endpoint(
        _fake_request({"X-Kavya-SmartPBX-Token": "test-token"})
    )["transfer_enabled"] is True


def test_unknown_service_mode_fails_closed():
    import server

    with pytest.raises(ValueError, match="invalid KAVYA_SERVICE_MODE"):
        server.build_service_app("both", {})


def test_smartpbx_session_declares_optional_diagnostic_sink_and_installs_before_welcome():
    import inspect

    parameters = inspect.signature(KavyaSmartPBXSession).parameters
    assert "diagnostic_sink" in parameters
    assert parameters["diagnostic_sink"].default is not inspect.Parameter.empty


@pytest.mark.asyncio
async def test_server_smartpbx_factory_declares_and_forwards_diagnostic_sink_contract():
    import inspect
    import server

    parameters = inspect.signature(server._new_smartpbx_session).parameters
    assert list(parameters) == ["context", "transport", "diagnostic_sink"]

    sentinel_sink = lambda *_values: None
    session = await server._new_smartpbx_session(context(), FakeTransport(), sentinel_sink)
    assert getattr(session, "_diagnostic_sink", None) is sentinel_sink

@pytest.mark.asyncio
async def test_smartpbx_session_installs_default_noop_sink_on_pipeline_before_welcome():
    async def process_post_call(**_metadata):
        pass

    session, pipeline, _, _ = make_session(post_call_processor=process_post_call)
    await session.start()
    try:
        sink = getattr(pipeline, "_smartpbx_diagnostic_sink", None)
        assert callable(sink)
        assert pipeline.spoken == ["Welcome to Hatton Hills."]
    finally:
        await session.finish(False)


class SinkObservingPipeline(FakePipeline):
    def __init__(self):
        super().__init__()
        self.sinks_seen_at_speak = []
    async def _speak(self, text):
        self.sinks_seen_at_speak.append(getattr(self, "_smartpbx_diagnostic_sink", None))
        await super()._speak(text)


@pytest.mark.asyncio
async def test_default_diagnostic_sink_is_callable_before_welcome_speak():
    async def process_post_call(**_metadata):
        pass
    pipeline = SinkObservingPipeline()
    session, _, _, _ = make_session(post_call_processor=process_post_call, pipeline=pipeline)
    await session.start()
    try:
        assert pipeline.spoken == ["Welcome to Hatton Hills."]
        assert len(pipeline.sinks_seen_at_speak) == 1
        assert callable(pipeline.sinks_seen_at_speak[0])
    finally:
        await session.finish(False)


@pytest.mark.asyncio
async def test_explicit_diagnostic_sink_reaches_pipeline_before_welcome_by_identity():
    import inspect
    async def process_post_call(**_metadata):
        pass
    explicit_sink = lambda *_values: None
    parameters = inspect.signature(KavyaSmartPBXSession).parameters
    assert "diagnostic_sink" in parameters
    pipeline = SinkObservingPipeline()
    session = KavyaSmartPBXSession(
        context(), FakeTransport(), pipeline=pipeline, stt_factory=lambda **_kwargs: FakeSTT(),
        post_call_processor=process_post_call, welcome_text="Welcome to Hatton Hills.",
        llm_provider="claude", model="test-model", diagnostic_sink=explicit_sink,
    )
    await session.start()
    try:
        assert pipeline.sinks_seen_at_speak == [explicit_sink]
    finally:
        await session.finish(False)


@pytest.mark.asyncio
async def test_explicit_falsey_diagnostic_sink_is_preserved_and_used_before_welcome():
    from smartpbx_diagnostics import DiagnosticFailureClass, DiagnosticOutcome, DiagnosticStage

    class FalseySink:
        def __init__(self):
            self.calls = []

        def __bool__(self):
            return False

        def __call__(self, stage, outcome, failure_class):
            self.calls.append((stage, outcome, failure_class))

    async def process_post_call(**_metadata):
        pass

    explicit_sink = FalseySink()
    pipeline = SinkObservingPipeline()
    session = KavyaSmartPBXSession(
        context(), FakeTransport(), pipeline=pipeline, stt_factory=lambda **_kwargs: FakeSTT(),
        post_call_processor=process_post_call, welcome_text="Welcome to Hatton Hills.",
        llm_provider="claude", model="test-model", diagnostic_sink=explicit_sink,
    )
    await session.start()
    try:
        assert pipeline.sinks_seen_at_speak == [explicit_sink]
        pipeline._smartpbx_diagnostic_sink(
            DiagnosticStage.SESSION_START,
            DiagnosticOutcome.COMPLETED,
            DiagnosticFailureClass.NONE,
        )
        assert explicit_sink.calls == [(
            DiagnosticStage.SESSION_START,
            DiagnosticOutcome.COMPLETED,
            DiagnosticFailureClass.NONE,
        )]
    finally:
        await session.finish(False)


async def _native_one_event_stream(event):
    yield event


class NativeRecordingOpenAI:
    def __init__(self, events=None):
        self.requests = []
        self.events = events
        self.chat = SimpleNamespace(completions=self)

    async def create(self, **kwargs):
        self.requests.append(kwargs)
        if self.events is not None:
            self.events.append("openai-dispatch")
        return _native_one_event_stream(
            SimpleNamespace(choices=[SimpleNamespace(delta=SimpleNamespace(content="native response", tool_calls=None))])
        )


class NativeRecordingGeminiModels:
    def __init__(self, owner):
        self.owner = owner

    async def generate_content_stream(self, **kwargs):
        self.owner.requests.append(kwargs)
        if self.owner.events is not None:
            self.owner.events.append("gemini-dispatch")
        return _native_one_event_stream(
            SimpleNamespace(candidates=[SimpleNamespace(
                finish_reason=None,
                content=SimpleNamespace(parts=[SimpleNamespace(text="native response", function_call=None)]),
            )])
        )


class NativeRecordingGemini:
    def __init__(self, events=None):
        self.requests = []
        self.events = events
        self.aio = SimpleNamespace(models=NativeRecordingGeminiModels(self))


class NativeRecordingClaudeStream:
    def __init__(self, event):
        self.event = event

    async def __aenter__(self):
        return _native_one_event_stream(self.event)

    async def __aexit__(self, *_args):
        return False


class NativeRecordingClaudeMessages:
    def __init__(self, owner):
        self.owner = owner

    def stream(self, **kwargs):
        self.owner.requests.append(kwargs)
        if self.owner.events is not None:
            self.owner.events.append("claude-dispatch")
        return NativeRecordingClaudeStream(
            SimpleNamespace(type="content_block_delta", delta=SimpleNamespace(type="text_delta", text="native response"))
        )


class NativeRecordingClaude:
    def __init__(self, events=None):
        self.requests = []
        self.events = events
        self.messages = NativeRecordingClaudeMessages(self)


def native_provider_clients(events=None):
    return {
        "claude": NativeRecordingClaude(events),
        "gemini": NativeRecordingGemini(events),
        "openai": NativeRecordingOpenAI(events),
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("provider", ["claude", "gemini", "openai"])
async def test_media_session_uses_explicit_native_provider_model_and_schema(monkeypatch, provider):
    import server

    clients = native_provider_clients()
    schemas = {
        "claude": [{"name": "claude_native_tool"}],
        "gemini": [{"function_declarations": [{"name": "gemini_native_tool"}]}],
        "openai": [{"type": "function", "function": {"name": "openai_native_tool"}}],
    }
    factory_calls = []

    def provider_factory(name):
        def factory():
            factory_calls.append(name)
            return schemas[name]

        return factory

    monkeypatch.setattr(server, "get_tools", provider_factory("claude"))
    monkeypatch.setattr(server, "get_tools_gemini", provider_factory("gemini"))
    monkeypatch.setattr(server, "get_tools_openai", provider_factory("openai"))
    expected_tools = schemas[provider]
    pipeline = server.MediaStreamSession(
        websocket=None,
        lang="en",
        anthropic_client=clients["claude"],
        gemini_client=clients["gemini"],
        openai_client=clients["openai"],
        media_transport=FakeTransport(),
        llm_provider=provider,
        model=f"{provider}-instance-model",
    )

    async def no_speak(*_args, **_kwargs):
        return None

    monkeypatch.setattr(server, "retrieve_context", lambda _text: "")
    monkeypatch.setattr(pipeline, "_speak", no_speak)
    await pipeline._process_utterance_bound("provider-turn")

    assert pipeline.tools is expected_tools
    assert factory_calls == [provider]
    assert len(clients[provider].requests) == 1
    for other_provider, client in clients.items():
        if other_provider != provider:
            assert client.requests == []
    request = clients[provider].requests[0]
    assert request["model"] == f"{provider}-instance-model"
    if provider == "gemini":
        assert request["config"]["tools"] is expected_tools
    else:
        assert request["tools"] is expected_tools


@pytest.mark.asyncio
async def test_smartpbx_runtime_pipeline_forwards_resolved_provider_and_model(monkeypatch):
    import server

    captured = {}
    gemini_client = object()

    class CapturingPipeline:
        def __init__(self, **kwargs):
            captured.update(kwargs)

    monkeypatch.setattr(server, "LLM_PROVIDER", "claude")
    monkeypatch.setattr(server, "_get_anthropic_client", lambda: (_ for _ in ()).throw(AssertionError("wrong client")))
    monkeypatch.setattr(server, "_get_gemini_client", lambda: gemini_client)
    monkeypatch.setattr(server, "_get_client", lambda: (_ for _ in ()).throw(AssertionError("wrong client")))
    monkeypatch.setattr(server, "MediaStreamSession", CapturingPipeline)
    session = KavyaSmartPBXSession(
        context(), FakeTransport(), stt_factory=lambda **_kwargs: FakeSTT(),
        post_call_processor=lambda **_kwargs: asyncio.sleep(0), welcome_text="",
        llm_provider="gemini", model="adapter-instance-model",
    )

    session._load_runtime_defaults()

    assert captured["llm_provider"] == "gemini"
    assert captured["model"] == "adapter-instance-model"
    assert captured["gemini_client"] is gemini_client


@pytest.mark.asyncio
async def test_media_session_retrieves_reference_before_selected_provider_dispatch(monkeypatch):
    import server

    events = []
    clients = native_provider_clients(events)
    pipeline = server.MediaStreamSession(
        websocket=None,
        lang="en",
        openai_client=clients["openai"],
        media_transport=FakeTransport(),
        llm_provider="openai",
        model="rag-instance-model",
    )

    def retrieve(_text):
        events.append("retrieve")
        return "reference-marker"

    async def no_speak(*_args, **_kwargs):
        return None

    monkeypatch.setattr(server, "retrieve_context", retrieve)
    monkeypatch.setattr(pipeline, "_speak", no_speak)
    await pipeline._process_utterance_bound("guest-marker")

    assert events.index("retrieve") < events.index("openai-dispatch")
    assert "reference-marker" in str(clients["openai"].requests[0]["messages"])


@pytest.mark.asyncio
async def test_smartpbx_start_uses_exact_configured_stt_factory_with_private_english_inputs():
    async def process_post_call(**_metadata):
        return None

    calls = []
    stt = FakeSTT()

    def configured_factory(**kwargs):
        calls.append(kwargs)
        return stt

    pipeline = FakePipeline()
    session = KavyaSmartPBXSession(
        context(), FakeTransport(), pipeline=pipeline, stt_factory=configured_factory,
        post_call_processor=process_post_call, welcome_text="",
        llm_provider="claude", model="stt-instance-model",
    )

    await session.start()
    try:
        assert session._stt_factory is configured_factory
        assert len(calls) == 1
        assert calls[0]["lang"] == "en"
        assert calls[0]["privacy_safe"] is True
        assert calls[0]["on_final_result"].__self__ is pipeline
        assert calls[0]["on_interim_result"].__self__ is pipeline
    finally:
        await session.finish(False)


@pytest.mark.asyncio
async def test_smartpbx_runtime_invalid_provider_fails_before_client_pipeline_or_stt(monkeypatch, caplog):
    import server

    client_factory_calls = []
    pipeline_constructions = []
    stt_calls = []
    real_media_session = server.MediaStreamSession

    def recording_client_factory(name):
        def factory():
            client_factory_calls.append(name)
            return object()

        return factory

    def recording_media_session(*args, **kwargs):
        pipeline_constructions.append((args, kwargs))
        return real_media_session(*args, **kwargs)

    def configured_stt_factory(**kwargs):
        stt_calls.append(kwargs)
        return FakeSTT()

    async def process_post_call(**_metadata):
        return None

    monkeypatch.setattr(server, "_get_anthropic_client", recording_client_factory("claude"))
    monkeypatch.setattr(server, "_get_gemini_client", recording_client_factory("gemini"))
    monkeypatch.setattr(server, "_get_client", recording_client_factory("openai"))
    monkeypatch.setattr(server, "MediaStreamSession", recording_media_session)
    invalid_provider = "private-invalid-provider-sentinel-abcdefghijklmnopqrstuvwxyz"
    session = KavyaSmartPBXSession(
        context(), FakeTransport(), stt_factory=configured_stt_factory,
        post_call_processor=process_post_call, welcome_text="",
        llm_provider=invalid_provider, model="invalid-provider-model",
    )

    with caplog.at_level(logging.INFO):
        with pytest.raises(ValueError) as exc_info:
            await session.start()

    assert str(exc_info.value) == "invalid LLM provider"
    assert invalid_provider not in str(exc_info.value)
    assert invalid_provider not in caplog.text
    assert client_factory_calls == []
    assert pipeline_constructions == []
    assert stt_calls == []


@pytest.mark.asyncio
async def test_smartpbx_fully_injected_invalid_provider_fails_before_any_start_side_effect(caplog):
    private_provider = "private-injected-provider-sentinel-abcdefghijklmnopqrstuvwxyz"
    stt_calls = []
    pipeline = FakePipeline()

    def configured_stt_factory(**kwargs):
        stt_calls.append(kwargs)
        return FakeSTT()

    async def process_post_call(**_metadata):
        return None

    session = KavyaSmartPBXSession(
        context(), FakeTransport(), pipeline=pipeline, stt_factory=configured_stt_factory,
        post_call_processor=process_post_call, welcome_text="private-welcome-marker",
        llm_provider=private_provider, model="fully-injected-model",
    )

    with caplog.at_level(logging.INFO):
        with pytest.raises(ValueError) as exc_info:
            await session.start()

    assert str(exc_info.value) == "invalid LLM provider"
    assert private_provider not in str(exc_info.value)
    assert private_provider not in caplog.text
    assert stt_calls == []
    assert pipeline._stt is None
    assert pipeline.spoken == []
    assert getattr(pipeline, "_smartpbx_transfer_context", None) is None


# A fake stream that never awaits also never lets the per-sentence TTS tasks it
# spawns begin running, so a mid-stream failure would cancel work that had not
# actually started. Fixtures place this marker where the real stream would have
# blocked on the network, to make in-flight audio genuinely in flight.
YIELD_TO_EVENT_LOOP = SimpleNamespace(type="__test_yield_to_loop__")


async def _direct_tool_event_stream(events):
    for event in events:
        if event is YIELD_TO_EVENT_LOOP:
            await asyncio.sleep(0)
            continue
        yield event


def claude_message_delta(stop_reason, output_tokens):
    return SimpleNamespace(
        type="message_delta",
        delta=SimpleNamespace(stop_reason=stop_reason),
        usage=SimpleNamespace(output_tokens=output_tokens),
    )


def claude_message_stop():
    """The event that closes a Claude message. Its ABSENCE is the signal that
    a stream was cut off rather than finished."""
    return SimpleNamespace(type="message_stop")


def claude_terminal_events(stop_reason, output_tokens):
    """How EVERY real Claude stream ends: the model reports why the turn ended,
    then closes the message. Defined up here beside the stream driver because
    the generic round fixtures below must carry it -- a stream that lacks both
    of these events is, by the classifier's definition, an aborted one, so
    omitting it from a fixture would silently make every ordinary round a
    transport failure rather than the ordinary round the test means to describe.
    """
    return [claude_message_delta(stop_reason, output_tokens), claude_message_stop()]


class DirectToolOpenAI:
    def __init__(self, rounds):
        self.rounds = rounds
        self.requests = []
        self.chat = SimpleNamespace(completions=self)

    async def create(self, **kwargs):
        self.requests.append(kwargs)
        return _direct_tool_event_stream(self.rounds.pop(0))


class DirectToolGeminiModels:
    def __init__(self, owner):
        self.owner = owner

    async def generate_content_stream(self, **kwargs):
        self.owner.requests.append(kwargs)
        return _direct_tool_event_stream(self.owner.rounds.pop(0))


class DirectToolGemini:
    def __init__(self, rounds):
        self.rounds = rounds
        self.requests = []
        self.aio = SimpleNamespace(models=DirectToolGeminiModels(self))


class DirectToolClaudeStream:
    def __init__(self, events):
        self.events = events

    async def __aenter__(self):
        return _direct_tool_event_stream(self.events)

    async def __aexit__(self, *_args):
        return False


class DirectToolClaudeMessages:
    def __init__(self, owner):
        self.owner = owner

    def stream(self, **kwargs):
        self.owner.requests.append(kwargs)
        return DirectToolClaudeStream(self.owner.rounds.pop(0))


class DirectToolClaude:
    def __init__(self, rounds):
        self.rounds = rounds
        self.requests = []
        self.messages = DirectToolClaudeMessages(self)


def direct_tool_round(provider, arguments, preamble=None, tool_name="create_booking"):
    if provider == "openai":
        return [SimpleNamespace(choices=[SimpleNamespace(delta=SimpleNamespace(
            content=preamble,
            tool_calls=[SimpleNamespace(
                index=0, id="tool-1",
                function=SimpleNamespace(name=tool_name, arguments=json.dumps(arguments)),
            )],
        ))])]
    if provider == "gemini":
        parts = []
        if preamble is not None:
            parts.append(SimpleNamespace(text=preamble, function_call=None))
        parts.append(SimpleNamespace(
            text=None,
            function_call=SimpleNamespace(name=tool_name, args=arguments),
        ))
        return [SimpleNamespace(candidates=[SimpleNamespace(
            finish_reason=None, content=SimpleNamespace(parts=parts),
        )])]
    events = []
    if preamble is not None:
        events.append(SimpleNamespace(
            type="content_block_delta",
            delta=SimpleNamespace(type="text_delta", text=preamble),
        ))
    events.extend([
        SimpleNamespace(
            type="content_block_start",
            content_block=SimpleNamespace(type="tool_use", id="tool-1", name=tool_name),
        ),
        SimpleNamespace(
            type="content_block_delta",
            delta=SimpleNamespace(type="input_json_delta", partial_json=json.dumps(arguments)),
        ),
        SimpleNamespace(type="content_block_stop"),
    ])
    # A real tool round reports `stop_reason="tool_use"` and closes the message.
    events.extend(claude_terminal_events("tool_use", 180))
    return events


def direct_text_round(provider, text):
    if provider == "openai":
        return [SimpleNamespace(choices=[SimpleNamespace(
            delta=SimpleNamespace(content=text, tool_calls=None),
        )])]
    if provider == "gemini":
        return [SimpleNamespace(candidates=[SimpleNamespace(
            finish_reason=None,
            content=SimpleNamespace(parts=[SimpleNamespace(text=text, function_call=None)]),
        )])]
    return [
        SimpleNamespace(
            type="content_block_delta",
            delta=SimpleNamespace(type="text_delta", text=text),
        ),
        # An ordinary answered turn ends on `end_turn`, not on the socket going
        # quiet.
        *claude_terminal_events("end_turn", 120),
    ]


def direct_tool_client(provider, rounds):
    if provider == "openai":
        return DirectToolOpenAI(rounds)
    if provider == "gemini":
        return DirectToolGemini(rounds)
    return DirectToolClaude(rounds)


class ControlledDirectToolOpenAI:
    """One shared client whose old stream can pause behind a newer turn."""

    def __init__(self, rounds):
        self.rounds = rounds
        self.requests = []
        self.chat = SimpleNamespace(completions=self)

    async def create(self, **kwargs):
        self.requests.append(kwargs)
        return self.rounds.pop(0)


class ControlledDirectToolGeminiModels:
    def __init__(self, owner):
        self.owner = owner

    async def generate_content_stream(self, **kwargs):
        self.owner.requests.append(kwargs)
        return self.owner.rounds.pop(0)


class ControlledDirectToolGemini:
    def __init__(self, rounds):
        self.rounds = rounds
        self.requests = []
        self.aio = SimpleNamespace(models=ControlledDirectToolGeminiModels(self))


class ControlledDirectToolClaudeStream:
    def __init__(self, events):
        self.events = events

    async def __aenter__(self):
        return self.events

    async def __aexit__(self, *_args):
        return False


class ControlledDirectToolClaudeMessages:
    def __init__(self, owner):
        self.owner = owner

    def stream(self, **kwargs):
        self.owner.requests.append(kwargs)
        return ControlledDirectToolClaudeStream(self.owner.rounds.pop(0))


class ControlledDirectToolClaude:
    def __init__(self, rounds):
        self.rounds = rounds
        self.requests = []
        self.messages = ControlledDirectToolClaudeMessages(self)


def controlled_direct_tool_client(provider, rounds):
    if provider == "openai":
        return ControlledDirectToolOpenAI(rounds)
    if provider == "gemini":
        return ControlledDirectToolGemini(rounds)
    return ControlledDirectToolClaude(rounds)


def direct_tool_pipeline(server, provider, client, lang="en"):
    pipeline = server.MediaStreamSession(
        websocket=None, lang=lang, media_transport=FakeTransport(),
        anthropic_client=client if provider == "claude" else None,
        gemini_client=client if provider == "gemini" else None,
        openai_client=client if provider == "openai" else None,
        llm_provider=provider, model=f"{provider}-tool-model",
    )
    pipeline._smartpbx_transfer_context = object()
    pipeline.tools = [{"provider": provider}]
    return pipeline


def direct_tool_history_records(provider, history):
    """Return provider-normalized request/result identifiers from shared history."""
    if provider == "claude":
        requests = [
            block
            for message in history
            if message.get("role") == "assistant"
            and isinstance(message.get("content"), list)
            for block in message["content"]
            if isinstance(block, dict) and block.get("type") == "tool_use"
        ]
        results = [
            block
            for message in history
            if message.get("role") == "user"
            and isinstance(message.get("content"), list)
            for block in message["content"]
            if isinstance(block, dict) and block.get("type") == "tool_result"
        ]
        return (
            [request["id"] for request in requests],
            [request["name"] for request in requests],
            [result["tool_use_id"] for result in results],
        )

    requests = [
        call
        for message in history
        if message.get("role") == "assistant"
        for call in message.get("tool_calls") or []
    ]
    results = [
        message
        for message in history
        if message.get("role") == "tool"
    ]
    return (
        [request["id"] for request in requests],
        [request["function"]["name"] for request in requests],
        [result["tool_call_id"] for result in results],
    )


def direct_single_tool_pairs(provider, history):
    """Return each adjacent provider request/result pair with its input."""
    pairs = []
    for index, message in enumerate(history[:-1]):
        following = history[index + 1]
        if provider == "claude":
            requests = [
                block
                for block in message.get("content", [])
                if isinstance(block, dict) and block.get("type") == "tool_use"
            ]
            results = [
                block
                for block in following.get("content", [])
                if isinstance(block, dict) and block.get("type") == "tool_result"
            ]
            for request in requests:
                matching = [
                    result for result in results
                    if result.get("tool_use_id") == request["id"]
                ]
                if matching:
                    pairs.append((request["input"], request["id"], matching[0]["tool_use_id"]))
            continue

        for request in message.get("tool_calls") or []:
            if following.get("role") == "tool" and following.get("tool_call_id") == request["id"]:
                pairs.append((
                    json.loads(request["function"]["arguments"]),
                    request["id"],
                    following["tool_call_id"],
                ))
    return pairs


def direct_tool_request_inputs(provider, history):
    """Return every provider request input, including unmatched requests."""
    if provider == "claude":
        return [
            block["input"]
            for message in history
            if message.get("role") == "assistant"
            and isinstance(message.get("content"), list)
            for block in message["content"]
            if isinstance(block, dict) and block.get("type") == "tool_use"
        ]
    return [
        json.loads(call["function"]["arguments"])
        for message in history
        if message.get("role") == "assistant"
        for call in message.get("tool_calls") or []
    ]


class CapturingTextWebSocket:
    def __init__(self):
        self.messages = []

    async def send_text(self, message):
        self.messages.append(message)


@pytest.mark.asyncio
async def test_direct_english_capture_tool_uses_last_raw_utterance_for_number_and_name(monkeypatch):
    import server

    client = direct_tool_client("openai", [
        direct_tool_round("openai", {"spoken": "zero one"}, tool_name="capture_spoken_number"),
        direct_text_round("openai", "Thanks, got the number."),
        direct_tool_round("openai", {"spoken": "bad-model-name"}, tool_name="capture_spoken_name"),
        direct_text_round("openai", "Thanks, got the name."),
    ])
    pipeline = direct_tool_pipeline(server, "openai", client)
    captured: list[tuple[str, str]] = []

    async def successful_tool(name, arguments):
        captured.append((name, arguments.get("spoken")))
        return json.dumps({"status": "ok"})

    monkeypatch.setattr(server, "retrieve_context", lambda _text: "")
    monkeypatch.setattr(server, "execute_tool", successful_tool)
    await pipeline._process_utterance_bound("double seven")
    await pipeline._process_utterance_bound("Jane Doe")

    assert captured == [
        ("capture_spoken_number", "double seven"),
        ("capture_spoken_name", "Jane Doe"),
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("provider", ["openai", "gemini", "claude"])
@pytest.mark.parametrize("entrypoint", ["provider", "bound"])
async def test_direct_smartpbx_runner_without_turn_contract_keeps_legacy_tool_behavior(
    monkeypatch, provider, entrypoint,
):
    """Injected callers may have no ContextVar or no telemetry-owned turn ID."""
    import server

    client = direct_tool_client(provider, [
        direct_tool_round(provider, {"request": "legacy"}),
        direct_text_round(provider, "Legacy turn complete."),
    ])
    pipeline = direct_tool_pipeline(server, provider, client)
    pipeline.history = [{"role": "user", "content": "legacy caller request"}]
    executed: list[tuple[str, dict[str, object]]] = []
    tool_contexts: list[tuple[bool, str | None]] = []

    async def record_tool(name, arguments):
        runner = server._smartpbx_runner_context.get()
        tool_contexts.append((runner is not None, None if runner is None else runner.turn_id))
        executed.append((name, dict(arguments)))
        return json.dumps({"status": "ok"})

    async def no_speak(*_args, **_kwargs):
        return None

    monkeypatch.setattr(server, "execute_tool", record_tool)
    monkeypatch.setattr(pipeline, "_speak", no_speak)
    monkeypatch.setattr(server, "retrieve_context", lambda _text: "")

    if entrypoint == "bound":
        await pipeline._process_utterance_bound("legacy caller request")
    else:
        assert server._smartpbx_runner_context.get() is None
        if provider == "openai":
            await pipeline._run_llm()
        elif provider == "gemini":
            await pipeline._run_llm_gemini()
        else:
            await pipeline._run_llm_claude()

    assert executed == [("create_booking", {"request": "legacy"})]
    assert tool_contexts == [
        (entrypoint == "bound", None)
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("provider", ["openai", "gemini", "claude"])
@pytest.mark.parametrize(
    "tool_name",
    ["create_booking", "transfer_to_human", "capture_spoken_number"],
)
async def test_barged_in_stale_runner_cannot_execute_tools_or_consume_new_raw_utterance(
    monkeypatch, provider, tool_name,
):
    """Only the current SmartPBX runner may cross the tool side-effect boundary."""
    import server

    old_stream_blocked = asyncio.Event()
    release_old_stream = asyncio.Event()
    old_turns = iter(("old-turn", "new-turn"))

    old_arguments = (
        {"spoken": "model-old-spoken"}
        if tool_name == "capture_spoken_number"
        else {"request": "old"}
    )
    new_arguments = (
        {"spoken": "model-new-spoken"}
        if tool_name == "capture_spoken_number"
        else {"request": "new"}
    )

    async def delayed_old_tool_round():
        for event in direct_tool_round(provider, old_arguments, tool_name=tool_name):
            yield event
        old_stream_blocked.set()
        await release_old_stream.wait()

    async def immediate_round(events):
        for event in events:
            yield event

    client = controlled_direct_tool_client(provider, [
        delayed_old_tool_round(),
        immediate_round(direct_tool_round(provider, new_arguments, tool_name=tool_name)),
        immediate_round(direct_text_round(provider, "New turn complete.")),
        immediate_round(direct_text_round(provider, "Old turn complete.")),
    ])
    pipeline = direct_tool_pipeline(server, provider, client)
    telemetry = server.SmartPBXTurnTelemetry(new_id=lambda: next(old_turns))
    pipeline._turn_telemetry = telemetry
    old_turn = telemetry.start_turn("final")
    pipeline._active_smartpbx_turn_id = old_turn
    executed: list[tuple[str, dict[str, object]]] = []

    async def record_tool(name, arguments):
        executed.append((name, dict(arguments)))
        return json.dumps({"status": "ok"})

    async def no_speak(*_args, **_kwargs):
        return None

    monkeypatch.setattr(server, "retrieve_context", lambda _text: "")
    monkeypatch.setattr(server, "execute_tool", record_tool)
    monkeypatch.setattr(pipeline, "_speak", no_speak)

    old_task = asyncio.create_task(
        pipeline._process_utterance_bound("old guest raw utterance")
    )
    await asyncio.wait_for(old_stream_blocked.wait(), timeout=1)

    await pipeline._handle_bargein()
    new_turn = telemetry.start_turn("final")
    pipeline._active_smartpbx_turn_id = new_turn
    new_task = asyncio.create_task(
        pipeline._process_utterance_bound("new guest raw utterance")
    )
    await asyncio.wait_for(new_task, timeout=1)

    expected_arguments = (
        {"spoken": "new guest raw utterance"}
        if tool_name == "capture_spoken_number"
        else new_arguments
    )
    assert executed == [(tool_name, expected_arguments)]
    request_ids, request_names, result_ids = direct_tool_history_records(
        provider, pipeline.history,
    )
    assert request_names == [tool_name]
    assert result_ids == request_ids
    history_after_current = list(pipeline.history)
    transcript_after_current = list(pipeline.full_transcript)
    delivery_after_current = (
        pipeline._assistant_turn_generation,
        list(pipeline._assistant_turn_generated_sentences),
        list(pipeline._delivered_sentences),
        pipeline._track_assistant_turn_delivery,
        pipeline._last_guest_utterance_raw,
    )

    release_old_stream.set()
    await asyncio.wait_for(old_task, timeout=1)

    # The stale runner still holds its old provider stream, but must neither run
    # a side effect nor write an unmatched tool request to the newer turn.
    assert executed == [(tool_name, expected_arguments)]
    assert pipeline.history == history_after_current
    assert pipeline.full_transcript == transcript_after_current
    assert (
        pipeline._assistant_turn_generation,
        list(pipeline._assistant_turn_generated_sentences),
        list(pipeline._delivered_sentences),
        pipeline._track_assistant_turn_delivery,
        pipeline._last_guest_utterance_raw,
    ) == delivery_after_current


@pytest.mark.asyncio
@pytest.mark.parametrize("provider", ["openai", "gemini", "claude"])
async def test_barged_in_slow_tool_cannot_leave_orphaned_provider_history(
    monkeypatch, provider,
):
    """A completed old effect is not replayed or written after a newer turn wins."""
    import server

    old_tool_entered = asyncio.Event()
    release_old_tool = asyncio.Event()
    turn_ids = iter(("old-slow-tool", "new-slow-tool"))
    old_arguments = {"request": "old", "guest_name": "Old Guest"}
    new_arguments = {"request": "new", "guest_name": "Current Guest"}

    async def immediate_round(events):
        for event in events:
            yield event

    client = controlled_direct_tool_client(provider, [
        immediate_round(direct_tool_round(provider, old_arguments)),
        immediate_round(direct_tool_round(provider, new_arguments)),
        immediate_round(direct_text_round(provider, "Current tool complete.")),
    ])
    pipeline = direct_tool_pipeline(server, provider, client)
    telemetry = server.SmartPBXTurnTelemetry(new_id=lambda: next(turn_ids))
    pipeline._turn_telemetry = telemetry
    old_turn = telemetry.start_turn("final")
    pipeline._active_smartpbx_turn_id = old_turn
    executed: list[dict[str, object]] = []

    async def slow_old_tool(name, arguments):
        executed.append({"name": name, "arguments": dict(arguments)})
        if arguments["request"] == "old":
            old_tool_entered.set()
            await release_old_tool.wait()
        return json.dumps({"status": "ok"})

    async def no_speak(*_args, **_kwargs):
        return None

    monkeypatch.setattr(server, "retrieve_context", lambda _text: "")
    monkeypatch.setattr(server, "execute_tool", slow_old_tool)
    monkeypatch.setattr(pipeline, "_speak", no_speak)

    old_task = asyncio.create_task(
        pipeline._process_utterance_bound("old slow tool utterance")
    )
    await asyncio.wait_for(old_tool_entered.wait(), timeout=1)

    await pipeline._handle_bargein()
    new_turn = telemetry.start_turn("final")
    pipeline._active_smartpbx_turn_id = new_turn
    await asyncio.wait_for(
        pipeline._process_utterance_bound("new tool utterance"), timeout=1,
    )

    assert executed == [
        {"name": "create_booking", "arguments": old_arguments},
        {"name": "create_booking", "arguments": new_arguments},
    ]
    assert direct_tool_request_inputs(provider, pipeline.history) == [new_arguments]
    request_ids, request_names, result_ids = direct_tool_history_records(
        provider, pipeline.history,
    )
    assert request_names == ["create_booking"]
    assert result_ids == request_ids
    assert direct_single_tool_pairs(provider, pipeline.history) == [
        (new_arguments, "tool-1" if provider != "gemini" else "gemini_tc_0_0",
         "tool-1" if provider != "gemini" else "gemini_tc_0_0"),
    ]
    history_after_current = list(pipeline.history)
    transcript_after_current = list(pipeline.full_transcript)
    delivery_after_current = (
        pipeline._assistant_turn_generation,
        list(pipeline._assistant_turn_generated_sentences),
        list(pipeline._delivered_sentences),
        pipeline._track_assistant_turn_delivery,
        pipeline._last_guest_utterance_raw,
        dict(pipeline._booking_slots),
    )

    release_old_tool.set()
    await asyncio.wait_for(old_task, timeout=1)

    assert direct_single_tool_pairs(provider, pipeline.history) == [
        (new_arguments, "tool-1" if provider != "gemini" else "gemini_tc_0_0",
         "tool-1" if provider != "gemini" else "gemini_tc_0_0"),
    ]
    assert direct_tool_request_inputs(provider, pipeline.history) == [new_arguments]
    request_ids, request_names, result_ids = direct_tool_history_records(
        provider, pipeline.history,
    )
    assert request_names == ["create_booking"]
    assert result_ids == request_ids
    assert pipeline.history == history_after_current
    assert pipeline.full_transcript == transcript_after_current
    assert (
        pipeline._assistant_turn_generation,
        list(pipeline._assistant_turn_generated_sentences),
        list(pipeline._delivered_sentences),
        pipeline._track_assistant_turn_delivery,
        pipeline._last_guest_utterance_raw,
        dict(pipeline._booking_slots),
    ) == delivery_after_current


@pytest.mark.asyncio
@pytest.mark.parametrize("provider", ["openai", "gemini", "claude"])
async def test_barged_in_stale_text_runner_cannot_write_current_turn_state(
    monkeypatch, provider,
):
    """A stale no-tool completion cannot append history or transcript text."""
    import server

    old_stream_blocked = asyncio.Event()
    release_old_stream = asyncio.Event()
    turn_ids = iter(("old-text-turn", "new-text-turn"))

    async def delayed_old_text_round():
        for event in direct_text_round(provider, "Old stale response."):
            yield event
        old_stream_blocked.set()
        await release_old_stream.wait()

    async def immediate_round(events):
        for event in events:
            yield event

    client = controlled_direct_tool_client(provider, [
        delayed_old_text_round(),
        immediate_round(direct_text_round(provider, "New current response.")),
    ])
    pipeline = direct_tool_pipeline(server, provider, client)
    telemetry = server.SmartPBXTurnTelemetry(new_id=lambda: next(turn_ids))
    pipeline._turn_telemetry = telemetry
    old_turn = telemetry.start_turn("final")
    pipeline._active_smartpbx_turn_id = old_turn

    async def no_speak(*_args, **_kwargs):
        return None

    monkeypatch.setattr(server, "retrieve_context", lambda _text: "")
    monkeypatch.setattr(pipeline, "_speak", no_speak)

    old_task = asyncio.create_task(
        pipeline._process_utterance_bound("old text utterance")
    )
    await asyncio.wait_for(old_stream_blocked.wait(), timeout=1)

    await pipeline._handle_bargein()
    new_turn = telemetry.start_turn("final")
    pipeline._active_smartpbx_turn_id = new_turn
    await asyncio.wait_for(
        pipeline._process_utterance_bound("new text utterance"), timeout=1,
    )

    assert [
        message["content"]
        for message in pipeline.history
        if message.get("role") == "assistant" and isinstance(message.get("content"), str)
    ] == ["New current response."]
    assert [
        message["text"]
        for message in pipeline.full_transcript
        if message.get("role") == "assistant"
    ] == ["New current response."]
    history_after_current = list(pipeline.history)
    transcript_after_current = list(pipeline.full_transcript)
    delivery_after_current = (
        pipeline._assistant_turn_generation,
        list(pipeline._assistant_turn_generated_sentences),
        list(pipeline._delivered_sentences),
        pipeline._track_assistant_turn_delivery,
        pipeline._last_guest_utterance_raw,
    )

    release_old_stream.set()
    await asyncio.wait_for(old_task, timeout=1)

    assert pipeline.history == history_after_current
    assert pipeline.full_transcript == transcript_after_current
    assert (
        pipeline._assistant_turn_generation,
        list(pipeline._assistant_turn_generated_sentences),
        list(pipeline._delivered_sentences),
        pipeline._track_assistant_turn_delivery,
        pipeline._last_guest_utterance_raw,
    ) == delivery_after_current


@pytest.mark.asyncio
async def test_openai_streaming_capture_tool_falls_back_to_model_spoken_without_user_utterance(monkeypatch):
    import server

    socket = CapturingTextWebSocket()
    client = direct_tool_client("openai", [
        direct_tool_round("openai", {"spoken": "zero one two"}, tool_name="capture_spoken_number"),
        direct_text_round("openai", "Got it."),
    ])
    captured: list[dict[str, object]] = []

    async def successful_tool(name, arguments):
        captured.append(arguments)
        return json.dumps({"status": "ok"})

    monkeypatch.setattr(server, "execute_tool", successful_tool)
    await server._run_llm_streaming(
        client=client,
        system="You are a booking assistant.",
        conversation_history=[{"role": "assistant", "content": "No user turn in this turn."}],
        tools=server.get_tools(),
        websocket=socket,
    )

    assert captured == [{"spoken": "zero one two"}]


def test_extract_last_user_utterance_strips_reference_context_prefix():
    import server

    history = [
        {"role": "user", "content": "stale turn"},
        {"role": "assistant", "content": "assistant follows"},
        {"role": "user", "content": "[Reference context: context payload.]\n\nGuest: double seven"},
    ]

    assert server._extract_last_user_utterance(history) == "double seven"


@pytest.mark.asyncio
@pytest.mark.parametrize("provider", ["claude", "gemini", "openai"])
async def test_direct_english_tool_failure_uses_one_filler_and_opaque_recovery(monkeypatch, caplog, provider):
    import server

    private_arguments = {"private_tool_argument": "private-tool-argument-sentinel"}
    private_exception = "private-tool-exception-sentinel"
    recovery = "Recovery is ready."
    client = direct_tool_client(provider, [
        direct_tool_round(provider, private_arguments),
        direct_text_round(provider, recovery),
    ])
    pipeline = direct_tool_pipeline(server, provider, client)
    spoken = []
    order = []
    executions = []

    async def speak(text, generation=-1):
        spoken.append((text, generation))
        order.append("filler" if text == server.TOOL_FILLERS["create_booking"] else "speech")

    async def fail_tool(name, arguments):
        executions.append((name, arguments))
        order.append("execute")
        raise RuntimeError(private_exception)

    monkeypatch.setattr(server, "retrieve_context", lambda _text: "")
    monkeypatch.setattr(server, "execute_tool", fail_tool)
    monkeypatch.setattr(pipeline, "_speak", speak)
    with caplog.at_level(logging.INFO):
        await pipeline._process_utterance_bound("safe guest turn")

    filler = server.TOOL_FILLERS["create_booking"]
    assert spoken[0] == (filler, 0)
    assert sum(text == filler for text, _generation in spoken) == 1
    assert order.index("filler") < order.index("execute")
    assert [name for name, _arguments in executions] == ["create_booking"]
    assert recovery in [text for text, _generation in spoken]
    second_request = repr(client.requests[1])
    if provider == "gemini":
        response = client.requests[1]["contents"][-1]["parts"][0]["function_response"]["response"]
        assert response == {"error": "tool_execution_failed"}
    else:
        assert json.dumps({"error": "tool_execution_failed"}) in second_request
    for private_value in (private_exception, private_arguments["private_tool_argument"]):
        assert private_value not in second_request
        assert private_value not in repr(pipeline.history)
        assert private_value not in repr(pipeline.full_transcript)
        assert private_value not in caplog.text


@pytest.mark.asyncio
@pytest.mark.parametrize("provider", ["claude", "gemini", "openai"])
async def test_direct_english_tool_preamble_skips_canned_filler_and_recovers(monkeypatch, provider):
    import server

    private_arguments = {"private_tool_argument": "private-tool-argument-sentinel"}
    preamble = "I will take care of that."
    recovery = "Recovery is ready."
    client = direct_tool_client(provider, [
        direct_tool_round(provider, private_arguments, preamble=preamble),
        direct_text_round(provider, recovery),
    ])
    pipeline = direct_tool_pipeline(server, provider, client)
    spoken = []
    executions = []

    async def speak(text, generation=-1):
        spoken.append((text, generation))

    async def fail_tool(name, arguments):
        executions.append((name, arguments))
        raise RuntimeError("private-tool-exception-sentinel")

    monkeypatch.setattr(server, "retrieve_context", lambda _text: "")
    monkeypatch.setattr(server, "execute_tool", fail_tool)
    monkeypatch.setattr(pipeline, "_speak", speak)
    await pipeline._process_utterance_bound("safe guest turn")

    filler = server.TOOL_FILLERS["create_booking"]
    assert preamble in [text for text, _generation in spoken]
    assert filler not in [text for text, _generation in spoken]
    assert [name for name, _arguments in executions] == ["create_booking"]
    assert recovery in [text for text, _generation in spoken]


@pytest.mark.asyncio
@pytest.mark.parametrize("provider", ["claude", "gemini", "openai"])
async def test_direct_english_multiround_transcript_joins_text_with_existing_separator(monkeypatch, provider):
    import server

    client = direct_tool_client(provider, [
        direct_tool_round(provider, {"safe": "value"}, preamble="Preamble."),
        direct_text_round(provider, "Recovery."),
    ])
    pipeline = direct_tool_pipeline(server, provider, client)

    async def no_speak(*_args, **_kwargs):
        return None

    async def successful_tool(_name, _arguments):
        return "ok"

    monkeypatch.setattr(server, "retrieve_context", lambda _text: "")
    monkeypatch.setattr(server, "execute_tool", successful_tool)
    monkeypatch.setattr(pipeline, "_speak", no_speak)
    await pipeline._process_utterance_bound("safe guest turn")

    assert pipeline.full_transcript[-1] == {"role": "assistant", "text": "Preamble. Recovery."}


@pytest.mark.asyncio
async def test_retained_non_english_direct_tool_uses_existing_media_stream_filler(monkeypatch):
    import server

    client = direct_tool_client("openai", [
        direct_tool_round("openai", {"safe": "value"}),
        direct_text_round("openai", "Recovery."),
    ])
    pipeline = direct_tool_pipeline(server, "openai", client, lang="ta")
    spoken = []

    async def speak(text, generation=-1):
        spoken.append((text, generation))

    async def successful_tool(_name, _arguments):
        return "ok"

    monkeypatch.setattr(server, "retrieve_context", lambda _text: "")
    monkeypatch.setattr(server, "execute_tool", successful_tool)
    monkeypatch.setattr(pipeline, "_speak", speak)
    await pipeline._process_utterance_bound("safe guest turn")

    assert server.MEDIA_STREAM_FILLERS["ta"]["create_booking"] in [text for text, _generation in spoken]


@pytest.mark.asyncio
@pytest.mark.parametrize("provider", ["claude", "gemini", "openai"])
async def test_retained_non_english_multiround_transcript_preserves_legacy_concatenation(monkeypatch, provider):
    import server

    client = direct_tool_client(provider, [
        direct_tool_round(provider, {"safe": "value"}, preamble="Preamble."),
        direct_text_round(provider, "Recovery."),
    ])
    pipeline = direct_tool_pipeline(server, provider, client, lang="ta")

    async def no_speak(*_args, **_kwargs):
        return None

    async def successful_tool(_name, _arguments):
        return "ok"

    monkeypatch.setattr(server, "retrieve_context", lambda _text: "")
    monkeypatch.setattr(server, "execute_tool", successful_tool)
    monkeypatch.setattr(pipeline, "_speak", no_speak)
    await pipeline._process_utterance_bound("safe guest turn")

    assert pipeline.full_transcript[-1] == {"role": "assistant", "text": "Preamble.Recovery."}


@pytest.mark.asyncio
@pytest.mark.parametrize("tool_name", ["check_availability", "create_booking", "transfer_to_human"])
async def test_claude_streaming_tool_round_sends_core_fillers(monkeypatch, tool_name):
    import server

    client = direct_tool_client("claude", [
        direct_tool_round("claude", {"safe": "value"}, tool_name=tool_name),
        direct_text_round("claude", "Recovery."),
    ])
    websocket = CapturingTextWebSocket()

    async def successful_tool(_name, _arguments):
        return json.dumps({"status": "ok"})

    monkeypatch.setattr(server, "execute_tool", successful_tool)
    server._TOOL_FILLER_CYCLES[tool_name] = 0

    await server._run_llm_streaming_claude(
        client=client,
        system="You are a booking assistant.",
        conversation_history=[],
        tools=server.get_tools(),
        websocket=websocket,
        lang="en",
        generation_ref=[0],
        transcript_sink=[],
    )

    tokens = [json.loads(message)["token"] for message in websocket.messages]
    assert tokens and tokens[0] == server.TOOL_FILLER_VARIANTS[tool_name][0]


@pytest.mark.asyncio
async def test_claude_streaming_tool_round_filler_suppressed_after_preamble(monkeypatch):
    import server

    preamble = "I will take care of that."
    client = direct_tool_client("claude", [
        direct_tool_round(
            "claude",
            {"safe": "value"},
            preamble=preamble,
            tool_name="create_booking",
        ),
        direct_text_round("claude", "Recovery."),
    ])
    websocket = CapturingTextWebSocket()

    async def successful_tool(_name, _arguments):
        return json.dumps({"status": "ok"})

    monkeypatch.setattr(server, "execute_tool", successful_tool)
    await server._run_llm_streaming_claude(
        client=client,
        system="You are a booking assistant.",
        conversation_history=[],
        tools=server.get_tools(),
        websocket=websocket,
        lang="en",
        generation_ref=[0],
        transcript_sink=[],
    )

    tokens = [json.loads(message)["token"] for message in websocket.messages]
    assert tokens and tokens[0] == preamble
    assert preamble in tokens
    assert not any(
        token in set().union(*server.TOOL_FILLER_VARIANTS.values()) for token in tokens
    )


@pytest.mark.asyncio
async def test_claude_streaming_capture_tool_round_does_not_emit_filler(monkeypatch):
    import server

    client = direct_tool_client("claude", [
        direct_tool_round("claude", {"spoken": "zero one"}, tool_name="capture_spoken_number"),
        direct_text_round("claude", "Recovery."),
    ])
    websocket = CapturingTextWebSocket()

    async def successful_tool(_name, _arguments):
        return json.dumps({"status": "needs_more", "digits": "01"})

    monkeypatch.setattr(server, "execute_tool", successful_tool)
    await server._run_llm_streaming_claude(
        client=client,
        system="You are a booking assistant.",
        conversation_history=[],
        tools=server.get_tools(),
        websocket=websocket,
        lang="en",
        generation_ref=[0],
        transcript_sink=[],
    )

    tokens = [json.loads(message)["token"] for message in websocket.messages]
    assert tokens[0] == "Recovery."
    assert "Recovery." in tokens
    filler_set = {line for lines in server.TOOL_FILLER_VARIANTS.values() for line in lines}
    assert not any(token in filler_set for token in tokens)


@pytest.mark.asyncio
async def test_claude_streaming_tool_fillers_respect_stale_generation(monkeypatch):
    import server

    generation_ref = [0]
    first_round = direct_tool_round(
        "claude",
        {"safe": "value"},
        tool_name="check_availability",
    )
    second_round = []
    unblock = asyncio.Event()

    class _Stream:
        def __init__(self, events):
            self.events = events

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

        def __aiter__(self):
            return self

        async def __anext__(self):
            await unblock.wait()
            if not self.events:
                raise StopAsyncIteration
            return self.events.pop(0)

    class _ClaudeMessages:
        def __init__(self, rounds):
            self.rounds = rounds

        def stream(self, **_kwargs):
            return _Stream(self.rounds.pop(0))

    class _ClaudeClient:
        def __init__(self, rounds):
            self.rounds = rounds

        @property
        def messages(self):
            return _ClaudeMessages(self.rounds)

    client = _ClaudeClient([first_round, second_round])
    websocket = CapturingTextWebSocket()

    async def successful_tool(_name, _arguments):
        return json.dumps({"status": "ok"})

    monkeypatch.setattr(server, "execute_tool", successful_tool)
    run = asyncio.create_task(
        server._run_llm_streaming_claude(
            client=client,
            system="You are a booking assistant.",
            conversation_history=[],
            tools=server.get_tools(),
            websocket=websocket,
            lang="en",
            generation_ref=generation_ref,
            transcript_sink=[],
        )
    )

    await asyncio.sleep(0)
    generation_ref[0] = 1
    unblock.set()
    await run

    assert websocket.messages == []


@pytest.mark.asyncio
async def test_interrupted_smartpbx_turn_appends_only_delivered_prefix_to_history_and_transcript():
    import server

    transport = FakeTransport()
    pipeline = server.MediaStreamSession(
        websocket=None, lang="en", media_transport=transport
    )
    pipeline._smartpbx_transfer_context = object()

    pipeline._start_assistant_turn_delivery_tracking()
    generation = pipeline._assistant_turn_generation
    pipeline._record_generated_sentence("Welcome.")
    await pipeline._send_tts_done(sentence="Welcome.", turn_generation=generation)
    pipeline._record_generated_sentence("You have a special rate at this property.")
    # No completion callback for this sentence simulates a mid-sentence
    # interruption; it was generated but not actually spoken.
    pipeline._append_assistant_history({
        "role": "assistant",
        "content": "Welcome. You have a special rate at this property.",
    })
    pipeline._append_assistant_turn_to_transcript(
        "Welcome. You have a special rate at this property."
    )

    assert pipeline.history[-1] == {
        "role": "assistant",
        "content": "Welcome. [interrupted]",
    }
    assert pipeline.full_transcript[-1] == {
        "role": "assistant",
        "text": "Welcome. [interrupted]",
    }
    assert pipeline._delivered_sentences == ["Welcome."]


@pytest.mark.asyncio
async def test_uninterrupted_smartpbx_turn_appends_complete_text_to_history_and_transcript():
    import server

    transport = FakeTransport()
    pipeline = server.MediaStreamSession(
        websocket=None, lang="en", media_transport=transport
    )
    pipeline._smartpbx_transfer_context = object()

    pipeline._start_assistant_turn_delivery_tracking()
    generation = pipeline._assistant_turn_generation
    pipeline._record_generated_sentence("Welcome.")
    await pipeline._send_tts_done(sentence="Welcome.", turn_generation=generation)
    pipeline._record_generated_sentence("You have a special rate at this property.")
    await pipeline._send_tts_done(
        sentence="You have a special rate at this property.",
        turn_generation=generation,
    )
    pipeline._append_assistant_history({
        "role": "assistant",
        "content": "Welcome. You have a special rate at this property.",
    })
    pipeline._append_assistant_turn_to_transcript(
        "Welcome. You have a special rate at this property."
    )

    assert pipeline.history[-1] == {
        "role": "assistant",
        "content": "Welcome. You have a special rate at this property.",
    }
    assert pipeline.full_transcript[-1] == {
        "role": "assistant",
        "text": "Welcome. You have a special rate at this property.",
    }


@pytest.mark.asyncio
async def test_delivered_sentence_state_resets_between_turns():
    import server

    transport = FakeTransport()
    pipeline = server.MediaStreamSession(
        websocket=None, lang="en", media_transport=transport
    )
    pipeline._smartpbx_transfer_context = object()

    pipeline._start_assistant_turn_delivery_tracking()
    pipeline._record_generated_sentence("One.")
    await pipeline._send_tts_done(sentence="One.", turn_generation=pipeline._assistant_turn_generation)
    assert pipeline._assistant_turn_generated_sentences == ["One."]
    assert pipeline._delivered_sentences == ["One."]

    pipeline._start_assistant_turn_delivery_tracking()
    assert pipeline._assistant_turn_generated_sentences == []
    assert pipeline._delivered_sentences == []

    pipeline._record_generated_sentence("New turn sentence")
    pipeline._append_assistant_history({
        "role": "assistant",
        "content": "New turn sentence",
    })
    pipeline._append_assistant_turn_to_transcript("New turn sentence")

    assert pipeline.history[-1] == {
        "role": "assistant",
        "content": "[interrupted]",
    }
    assert pipeline.full_transcript[-1] == {
        "role": "assistant",
        "text": "[interrupted]",
    }


@pytest.mark.asyncio
async def test_mid_sentence_interruption_does_not_record_in_delivered_list():
    import server

    transport = FakeTransport()
    pipeline = server.MediaStreamSession(
        websocket=None, lang="en", media_transport=transport
    )
    pipeline._smartpbx_transfer_context = object()

    pipeline._start_assistant_turn_delivery_tracking()
    first = pipeline._assistant_turn_generation
    pipeline._record_generated_sentence("Welcome.")
    await pipeline._send_tts_done(sentence="Welcome.", turn_generation=first)
    pipeline._record_generated_sentence("This sentence is never spoken fully.")
    pipeline._append_assistant_history({
        "role": "assistant",
        "content": "Welcome. This sentence is never spoken fully.",
    })
    pipeline._append_assistant_turn_to_transcript(
        "Welcome. This sentence is never spoken fully.",
    )

    assert pipeline._assistant_turn_generated_sentences == [
        "Welcome.",
        "This sentence is never spoken fully.",
    ]
    assert pipeline._delivered_sentences == ["Welcome."]
    assert "never spoken" not in pipeline._delivered_sentences
    assert pipeline.history[-1]["content"] == "Welcome. [interrupted]"


def test_record_delivered_sentence_skips_gap_and_keeps_tail_match_order():
    import server

    pipeline = server.MediaStreamSession(
        websocket=None, lang="en", media_transport=FakeTransport()
    )
    pipeline._start_assistant_turn_delivery_tracking()
    generation = pipeline._assistant_turn_generation
    first = "Welcome."
    second = "You can also add a transfer."
    third = "Please confirm your details."

    pipeline._record_generated_sentence(first)
    pipeline._record_generated_sentence(second)
    pipeline._record_generated_sentence(third)
    pipeline._record_delivered_sentence(first, generation)
    pipeline._record_delivered_sentence(third, generation)

    assert pipeline._delivered_sentences == [first, third]


@pytest.mark.asyncio
async def test_direct_tts_done_bargein_during_mark_does_not_rearm_reprompt():
    import server

    transport = BlockingMarkTransport()
    pipeline = server.MediaStreamSession(websocket=None, lang="en", media_transport=transport)
    pipeline._smartpbx_transfer_context = object()
    pipeline._is_speaking = True
    starting_generation = pipeline._speak_generation
    completion = asyncio.create_task(pipeline._send_tts_done())

    try:
        await asyncio.wait_for(transport.mark_entered.wait(), timeout=1)
        await pipeline._handle_bargein()
        assert pipeline._speak_generation == starting_generation + 1
        assert transport.clears == 1

        transport.release_mark.set()
        await asyncio.wait_for(completion, timeout=1)

        assert transport.marks == ["tts_done"]
        assert pipeline._reprompt_task is None
        assert pipeline._is_speaking is False
    finally:
        transport.release_mark.set()
        await asyncio.gather(completion, return_exceptions=True)
        pipeline._cancel_reprompt()


@pytest.mark.asyncio
async def test_direct_tts_done_transfer_during_mark_does_not_rearm_reprompt():
    import server

    transport = BlockingMarkTransport()
    pipeline = server.MediaStreamSession(websocket=None, lang="en", media_transport=transport)
    pipeline._smartpbx_transfer_context = object()
    pipeline._is_speaking = True
    completion = asyncio.create_task(pipeline._send_tts_done())

    try:
        await asyncio.wait_for(transport.mark_entered.wait(), timeout=1)
        await pipeline.enter_transfer_pending()
        assert transport.clears == 1

        transport.release_mark.set()
        await asyncio.wait_for(completion, timeout=1)

        assert transport.marks == ["tts_done"]
        assert pipeline.transfer_pending is True
        assert pipeline._reprompt_task is None
        assert pipeline._is_speaking is False
    finally:
        transport.release_mark.set()
        await asyncio.gather(completion, return_exceptions=True)
        pipeline._cancel_reprompt()


@pytest.mark.asyncio
async def test_direct_bargein_rejects_stale_generation_but_allows_current_generation():
    import server

    transport = FakeTransport()
    pipeline = server.MediaStreamSession(websocket=None, lang="en", media_transport=transport)
    pipeline._smartpbx_transfer_context = object()
    pipeline._is_speaking = True
    old_generation = pipeline._speak_generation
    spoken = []

    async def tts(text):
        spoken.append(text)

    pipeline._tts_elevenlabs = tts
    await pipeline._handle_bargein()
    await pipeline._speak("stale", generation=old_generation)
    await pipeline._speak("current", generation=pipeline._speak_generation)

    assert transport.clears == 1
    assert spoken == ["current"]


@pytest.mark.asyncio
async def test_direct_reprompt_lifecycle_replaces_cancels_resets_and_suppresses_transfer(monkeypatch):
    import server

    pipeline = server.MediaStreamSession(websocket=None, lang="en", media_transport=FakeTransport())
    pipeline._smartpbx_transfer_context = object()
    pipeline._event_loop = asyncio.get_running_loop()
    monkeypatch.setattr(server, "SILENCE_REPROMPT_DELAY", 0)
    monkeypatch.setattr(server, "MAX_REPROMPTS", 1)
    nudged = asyncio.Event()
    spoken = []

    async def speak(text, generation=-1):
        spoken.append(text)
        nudged.set()

    pipeline._speak = speak
    await pipeline._send_tts_done()
    await asyncio.wait_for(nudged.wait(), timeout=1)
    assert len(spoken) == 1
    assert pipeline._reprompt_count == 1

    started = asyncio.Event()
    blocker = asyncio.Event()

    async def wait_for_reprompt():
        started.set()
        await blocker.wait()

    pipeline._reprompt_after_silence = wait_for_reprompt
    pipeline._schedule_reprompt()
    await asyncio.wait_for(started.wait(), timeout=1)
    first = pipeline._reprompt_task
    pipeline._schedule_reprompt()
    second = pipeline._reprompt_task
    await asyncio.gather(first, return_exceptions=True)
    assert first is not second
    assert first.cancelled()

    pipeline._reprompt_count = 1
    await pipeline._accumulate_transcript("final caller speech")
    await asyncio.gather(second, return_exceptions=True)
    assert pipeline._reprompt_task is None
    assert pipeline._reprompt_count == 0

    pipeline._schedule_reprompt()
    third = pipeline._reprompt_task
    pipeline._reprompt_count = 1
    await pipeline._set_transcript_interim("interim caller speech")
    await asyncio.gather(third, return_exceptions=True)
    assert pipeline._reprompt_task is None
    assert pipeline._reprompt_count == 0

    pipeline.transfer_pending = True
    pipeline._schedule_reprompt()
    assert pipeline._reprompt_task is None
    if pipeline._endpointing_handle:
        pipeline._endpointing_handle.cancel()
        pipeline._endpointing_handle = None
    blocker.set()


@pytest.mark.asyncio
async def test_direct_tts_done_mark_then_queued_bargein_cancels_reprompt():
    import server

    transport = BlockingMarkTransport()
    pipeline = server.MediaStreamSession(websocket=None, lang="en", media_transport=transport)
    pipeline._smartpbx_transfer_context = object()
    pipeline._event_loop = asyncio.get_running_loop()
    pipeline._is_speaking = True
    starting_generation = pipeline._speak_generation
    barge_finished = asyncio.Event()
    original_bargein = pipeline._handle_bargein

    async def tracked_bargein():
        try:
            await original_bargein()
        finally:
            barge_finished.set()

    pipeline._handle_bargein = tracked_bargein
    completion = asyncio.create_task(pipeline._send_tts_done())

    try:
        await asyncio.wait_for(transport.mark_entered.wait(), timeout=1)
        transport.release_mark.set()
        pipeline._on_stt_result("caller interrupted")
        await asyncio.wait_for(completion, timeout=1)
        await asyncio.wait_for(barge_finished.wait(), timeout=1)

        assert transport.marks == ["tts_done"]
        assert transport.clears == 1
        assert pipeline._speak_generation == starting_generation + 1
        assert pipeline._is_speaking is False
        assert pipeline._reprompt_task is None
    finally:
        transport.release_mark.set()
        await asyncio.gather(completion, return_exceptions=True)
        pipeline._cancel_reprompt()


def _fake_request(headers):
    import types

    return types.SimpleNamespace(headers=headers)


def test_smartpbx_status_requires_the_shared_token():
    import server
    from fastapi import HTTPException

    app = server.build_service_app("smartpbx", {
        "ENABLE_SMARTPBX_WSS": "true",
        "SMARTPBX_WS_TOKEN": "status-token",
        "SMARTPBX_ACCOUNT_ID": "account-1",
    })
    status = {route.path: route for route in app.routes}["/smartpbx/status"].endpoint

    allowed = status(_fake_request({"X-Kavya-SmartPBX-Token": "status-token"}))
    assert allowed["active_sessions"] == 0
    assert allowed["max_sessions"] == 4
    assert allowed["echo_rejections_total"] == 0

    # active_sessions vs the cap of 4 is a live occupancy oracle, and
    # admitted_total is a call-volume counter. Neither may be internet-readable.
    for headers in (
        {},
        {"X-Kavya-SmartPBX-Token": ""},
        {"X-Kavya-SmartPBX-Token": "wrong-token12"},
        {"X-Kavya-SmartPBX-Token": "status-toke"},
        {"X-Kavya-SmartPBX-Token": "status-tokenn"},
    ):
        with pytest.raises(HTTPException) as raised:
            status(_fake_request(headers))
        assert raised.value.status_code == 401
        assert "status-token" not in str(raised.value.detail or "")


def test_smartpbx_status_honours_a_renamed_auth_header():
    import server
    from fastapi import HTTPException

    app = server.build_service_app("smartpbx", {
        "ENABLE_SMARTPBX_WSS": "true",
        "SMARTPBX_WS_TOKEN": "status-token",
        "SMARTPBX_ACCOUNT_ID": "account-1",
        "SMARTPBX_AUTH_HEADER_NAME": "X-Renamed-Token",
    })
    status = {route.path: route for route in app.routes}["/smartpbx/status"].endpoint

    assert status(_fake_request({"X-Renamed-Token": "status-token"}))["enabled"] is True
    with pytest.raises(HTTPException):
        status(_fake_request({"X-Kavya-SmartPBX-Token": "status-token"}))


def test_smartpbx_health_stays_open_for_liveness_probes():
    import server

    app = server.build_service_app("smartpbx", {
        "ENABLE_SMARTPBX_WSS": "true",
        "SMARTPBX_WS_TOKEN": "status-token",
        "SMARTPBX_ACCOUNT_ID": "account-1",
    })
    health = {route.path: route for route in app.routes}["/health"].endpoint

    assert health() == {"status": "ok", "service_mode": "smartpbx"}


@pytest.mark.asyncio
@pytest.mark.parametrize("provider", ["claude", "gemini", "openai"])
async def test_direct_smartpbx_provider_requests_use_the_concise_output_budget(
    monkeypatch, provider
):
    """Only direct Dialog sessions get the caller-rhythm budget -- 120 tokens
    for OpenAI and Gemini, and the raised Claude-only canary budget for
    Claude, whose default adaptive thinking spends tokens before any visible
    block opens."""
    import server

    client = direct_tool_client(provider, [direct_text_round(provider, "Concise reply.")])
    pipeline = direct_tool_pipeline(server, provider, client)

    async def no_speak(*_args, **_kwargs):
        return None

    monkeypatch.setattr(server, "retrieve_context", lambda _text: "")
    monkeypatch.setattr(pipeline, "_speak", no_speak)
    await pipeline._process_utterance_bound("guest turn")

    request = client.requests[0]
    if provider == "gemini":
        assert request["config"]["max_output_tokens"] == 120
    elif provider == "claude":
        assert request["max_tokens"] == 600
        assert request["max_tokens"] == server.SMARTPBX_CLAUDE_MAX_TOKENS
    else:
        assert request["max_tokens"] == 120


def test_claude_smartpbx_budget_is_600_and_scoped_away_from_the_other_providers():
    """The canary raises Claude alone. The shared SmartPBX budget, its
    resolver and the global ConversationRelay budget must not move."""
    import server

    assert server.SMARTPBX_CLAUDE_MAX_TOKENS == 600
    assert server.SMARTPBX_MAX_TOKENS == 120
    assert server.MAX_TOKENS == 300
    assert server._resolve_smartpbx_max_tokens(None) == 120
    assert server._resolve_smartpbx_max_tokens("201") == 200


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (None, 600),
        ("", 600),
        ("199", 200),
        ("200", 200),
        ("600", 600),
        ("1024", 1024),
        ("4096", 1024),
        ("not-an-int", 600),
    ],
)
def test_claude_smartpbx_output_token_resolver_defaults_and_clamps(raw, expected):
    import server

    assert server._resolve_smartpbx_claude_max_tokens(raw) == expected


@pytest.mark.parametrize(
    (
        "text_content", "tool_use_blocks", "incomplete", "malformed",
        "stop_reason", "terminal", "expected",
    ),
    [
        # COMPLETED is reserved for a terminal, non-truncated, structurally
        # sound round that produced something.
        ("Hello.", [], False, False, "end_turn", True, "completed"),
        ("", [{"name": "check_availability"}], False, False, "tool_use", True, "completed"),
        ("Hi.", [{"name": "check_availability"}], False, False, "tool_use", True, "completed"),
        # Rule 1: max_tokens is truncation regardless of what survived. The
        # last three of these classified COMPLETED before the precedence fix.
        ("", [], True, False, "max_tokens", True, "max_tokens_truncated"),
        ("", [], False, False, "max_tokens", True, "max_tokens_truncated"),
        ("Let me check.", [], False, False, "max_tokens", True, "max_tokens_truncated"),
        ("", [{"name": "check_availability"}], False, False, "max_tokens", True,
         "max_tokens_truncated"),
        ("Let me check.", [{"name": "check_availability"}], False, False, "max_tokens", True,
         "max_tokens_truncated"),
        # Rule 2: an unterminated or malformed block under any OTHER stop
        # reason keeps its more specific label, and keeps it even when the
        # round also produced usable text or a complete sibling block.
        ("", [], True, False, None, True, "incomplete_tool_block"),
        ("", [], True, False, "end_turn", True, "incomplete_tool_block"),
        ("Let me check.", [], True, False, "end_turn", True, "incomplete_tool_block"),
        ("", [{"name": "check_availability"}], True, False, "tool_use", True,
         "incomplete_tool_block"),
        # A block cut off by an abrupt EOF is still described best as an
        # unterminated block, not as the generic transport label.
        ("", [], True, False, None, False, "incomplete_tool_block"),
        ("", [], False, True, "tool_use", True, "malformed_tool_json"),
        ("Let me check.", [], False, True, "tool_use", True, "malformed_tool_json"),
        # Rule 3: no message_delta and no message_stop is an aborted stream,
        # ahead of visible output. Both content-bearing rows classified
        # COMPLETED before the precedence fix.
        ("", [], False, False, None, False, "stream_aborted"),
        ("Let me check.", [], False, False, None, False, "stream_aborted"),
        ("", [{"name": "check_availability"}], False, False, None, False, "stream_aborted"),
        # Rule 5: terminal, sound, and genuinely empty.
        ("", [], False, False, "end_turn", True, "true_empty"),
        ("   ", [], False, False, None, True, "true_empty"),
    ],
)
def test_claude_round_outcome_classification_is_exhaustive(
    text_content, tool_use_blocks, incomplete, malformed, stop_reason, terminal, expected
):
    import server

    outcome = server._classify_claude_round_outcome(
        text_content=text_content,
        tool_use_blocks=tool_use_blocks,
        incomplete_tool_block=incomplete,
        malformed_tool_json=malformed,
        stop_reason=stop_reason,
        saw_terminal_metadata=terminal,
    )

    assert outcome is server.SmartPBXClaudeRoundOutcome(expected)
    assert outcome.value == expected


@pytest.mark.asyncio
@pytest.mark.parametrize("provider", ["claude", "gemini", "openai"])
async def test_non_smartpbx_provider_requests_keep_the_existing_output_budget(
    monkeypatch, provider
):
    """ConversationRelay and ordinary Media Streams must remain at 300 tokens."""
    import server

    client = direct_tool_client(provider, [direct_text_round(provider, "Legacy reply.")])
    pipeline = server.MediaStreamSession(
        websocket=None,
        lang="en",
        media_transport=FakeTransport(),
        anthropic_client=client if provider == "claude" else None,
        gemini_client=client if provider == "gemini" else None,
        openai_client=client if provider == "openai" else None,
        llm_provider=provider,
        model=f"{provider}-legacy-model",
    )

    async def no_speak(*_args, **_kwargs):
        return None

    monkeypatch.setattr(server, "retrieve_context", lambda _text: "")
    monkeypatch.setattr(pipeline, "_speak", no_speak)
    await pipeline._process_utterance_bound("guest turn")

    request = client.requests[0]
    if provider == "gemini":
        assert request["config"]["max_output_tokens"] == 300
    else:
        assert request["max_tokens"] == 300


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (None, 120),
        ("", 120),
        ("39", 40),
        ("40", 40),
        ("120", 120),
        ("200", 200),
        ("201", 200),
        ("not-an-int", 120),
    ],
)
def test_smartpbx_output_token_resolver_defaults_and_clamps(raw, expected):
    import server

    assert server._resolve_smartpbx_max_tokens(raw) == expected


@pytest.mark.parametrize("raw", ["nan", "NaN", "inf", "-inf"])
def test_smartpbx_initial_filler_delay_rejects_nonfinite_environment_values(raw):
    import math
    import server

    resolved = server._resolve_smartpbx_initial_filler_delay(raw)

    assert math.isfinite(resolved)
    assert resolved == 1.5


class _ControlledInitialFillerSleep:
    def __init__(self):
        self.delays = []
        self.entered = asyncio.Event()
        self.release = asyncio.Event()

    async def __call__(self, seconds):
        self.delays.append(seconds)
        self.entered.set()
        await self.release.wait()


def _initial_filler_controller(server, sleep, speak, *, clear_audio=None):
    return server.SmartPBXInitialFillerController(
        speak=speak,
        generation=7,
        delay_seconds=2.5,
        sleep=sleep,
        clear_audio=clear_audio,
    )


@pytest.mark.asyncio
async def test_initial_smartpbx_filler_waits_exactly_2_5_seconds_then_speaks_once():
    import server

    sleep = _ControlledInitialFillerSleep()
    spoken = []

    async def speak(text, *, generation):
        spoken.append((text, generation))

    controller = _initial_filler_controller(server, sleep, speak)
    controller.start()
    await asyncio.sleep(0)
    assert sleep.delays == [2.5]
    assert spoken == []

    sleep.release.set()
    await controller.wait()

    assert spoken == [(server.SMARTPBX_INITIAL_FILLER_TEXT, 7)]
    assert controller.spoke is True
    assert controller.suppress_specialized_tool_filler is True


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "cancel",
    [
        lambda controller: controller.on_content_delta(),
        lambda controller: controller.on_tool_delta(),
        lambda controller: controller.on_barge_in(),
        lambda controller: controller.on_generation_change(8),
        lambda controller: controller.on_session_finish(),
    ],
    ids=["content", "tool", "barge-in", "generation-change", "finish"],
)
async def test_initial_smartpbx_filler_cancels_before_the_delay_for_every_terminal_race(
    cancel,
):
    import server

    sleep = _ControlledInitialFillerSleep()
    spoken = []

    async def speak(text, *, generation):
        spoken.append((text, generation))

    controller = _initial_filler_controller(server, sleep, speak)
    controller.start()
    await asyncio.sleep(0)
    await cancel(controller)
    sleep.release.set()
    await controller.wait()

    assert spoken == []
    assert controller.spoke is False
    assert controller.suppress_specialized_tool_filler is False


@pytest.mark.asyncio
@pytest.mark.parametrize("tool_name", ["create_booking", "transfer_to_human"])
async def test_spoken_initial_filler_drains_before_a_side_effecting_tool_runs(tool_name):
    import server

    sleep = _ControlledInitialFillerSleep()
    filler_started = asyncio.Event()
    filler_release = asyncio.Event()
    events = []

    async def speak(_text, *, generation):
        events.append(("filler", generation))
        filler_started.set()
        await filler_release.wait()
        events.append(("filler_drained", generation))

    async def clear_audio():
        events.append(("clear", None))

    controller = _initial_filler_controller(server, sleep, speak, clear_audio=clear_audio)
    controller.start()
    await asyncio.sleep(0)
    sleep.release.set()
    await asyncio.wait_for(filler_started.wait(), timeout=1)

    handoff = asyncio.create_task(controller.on_tool_delta())
    await asyncio.sleep(0)
    assert not handoff.done(), (
        "a tool delta must wait for already-started filler delivery, not cancel it"
    )
    assert events == [("filler", 7)]

    filler_release.set()
    await asyncio.wait_for(handoff, timeout=1)
    events.append((tool_name, None))

    assert events == [("filler", 7), ("filler_drained", 7), (tool_name, None)]
    assert not events.count(("clear", None)), "model readiness is not a barge-in"
    assert controller.suppress_specialized_tool_filler is True


@pytest.mark.asyncio
async def test_spoken_initial_filler_drains_before_content_handoff_returns():
    """A real answer waits behind an already-delivering initial filler."""
    import server

    sleep = _ControlledInitialFillerSleep()
    filler_started = asyncio.Event()
    filler_release = asyncio.Event()
    events = []

    async def speak(_text, *, generation):
        events.append(("filler", generation))
        filler_started.set()
        await filler_release.wait()
        events.append(("filler_drained", generation))

    async def clear_audio():
        events.append(("clear", None))

    controller = _initial_filler_controller(server, sleep, speak, clear_audio=clear_audio)
    controller.start()
    await asyncio.sleep(0)
    sleep.release.set()
    await asyncio.wait_for(filler_started.wait(), timeout=1)

    answer_handoff = asyncio.create_task(controller.on_content_delta())
    await asyncio.sleep(0)
    assert not answer_handoff.done(), (
        "the answer must wait for filler completion instead of truncating it"
    )
    assert events == [("filler", 7)]

    filler_release.set()
    await asyncio.wait_for(answer_handoff, timeout=1)
    events.append(("answer", 7))

    assert events == [("filler", 7), ("filler_drained", 7), ("answer", 7)]
    assert ("clear", None) not in events


@pytest.mark.asyncio
@pytest.mark.parametrize("capture", ["capture_spoken_number", "capture_spoken_name", "collect_number_via_keypad"])
async def test_capture_and_keypad_rounds_never_arm_an_initial_smartpbx_filler(capture):
    import server

    sleep = _ControlledInitialFillerSleep()
    spoken = []

    async def speak(text, *, generation):
        spoken.append((text, generation))

    controller = _initial_filler_controller(server, sleep, speak)
    controller.start(capture_tool=capture)
    await asyncio.sleep(0)
    sleep.release.set()
    await controller.wait()

    assert sleep.delays == []
    assert spoken == []


# ---------------------------------------------------------------------------
# Phase B: shared empty-response policy, stream timeouts, filler default (§5)
# ---------------------------------------------------------------------------

def direct_empty_round(provider):
    """A provider round with no text and no tool call at all.

    For Claude that still means a TERMINATED stream: the model reported ending
    its turn having emitted nothing, which is the genuinely-empty case this
    policy exists for. Leaving the terminal metadata off would describe a
    dropped connection instead, and the empty-response policy under test is not
    the transport-failure one.
    """
    if provider == "claude":
        return list(claude_terminal_events("end_turn", 8))
    return []


def _patch_direct_hanging_stream(monkeypatch, pipeline, provider, *, before_events=()):
    """Make this pipeline's provider stream yield ``before_events`` then hang
    forever, so the timeout guard (not real content) ends the round.

    Callers must monkeypatch the SMARTPBX_LLM_*_TIMEOUT_SECONDS constants to a
    small value first, or this would genuinely wait out the default 8s.
    """

    async def hang(*_events):
        for event in _events:
            yield event
        await asyncio.Event().wait()  # never set: only the wait_for timeout ends this

    if provider == "openai":
        async def create(**_kwargs):
            pipeline.client.requests.append(_kwargs)
            return hang(*before_events)
        monkeypatch.setattr(pipeline.client, "create", create)
    elif provider == "gemini":
        async def generate_content_stream(**_kwargs):
            pipeline.gemini_client.requests.append(_kwargs)
            return hang(*before_events)
        monkeypatch.setattr(
            pipeline.gemini_client.aio.models, "generate_content_stream", generate_content_stream
        )
    else:
        class _HangingClaudeStream:
            async def __aenter__(self):
                return hang(*before_events)

            async def __aexit__(self, *_args):
                return False

        def stream(**_kwargs):
            pipeline.anthropic_client.requests.append(_kwargs)
            return _HangingClaudeStream()

        monkeypatch.setattr(pipeline.anthropic_client.messages, "stream", stream)


def _patch_direct_hanging_acquisition(monkeypatch, pipeline, provider):
    """Make the ACQUISITION call itself hang forever — the awaited client
    call that creates the stream (OpenAI/Gemini) or enters the async context
    manager (Claude) never returns, as opposed to returning promptly and then
    stalling on the first item.

    Exercises the item-1 fix: acquisition must be bounded by the SAME
    initial-response deadline as the wait for the first delta, not left
    unguarded before the per-item timeout guard even starts.

    Callers must monkeypatch the SMARTPBX_LLM_*_TIMEOUT_SECONDS constants to
    a small value first, or this would genuinely wait out the default 8s.
    """
    if provider == "openai":
        async def create(**_kwargs):
            pipeline.client.requests.append(_kwargs)
            await asyncio.Event().wait()  # never set: only wait_for ends this

        monkeypatch.setattr(pipeline.client, "create", create)
    elif provider == "gemini":
        async def generate_content_stream(**_kwargs):
            pipeline.gemini_client.requests.append(_kwargs)
            await asyncio.Event().wait()

        monkeypatch.setattr(
            pipeline.gemini_client.aio.models, "generate_content_stream", generate_content_stream
        )
    else:
        class _HangingAcquisitionClaudeStream:
            async def __aenter__(self):
                await asyncio.Event().wait()

            async def __aexit__(self, *_args):
                return False

        def stream(**_kwargs):
            pipeline.anthropic_client.requests.append(_kwargs)
            return _HangingAcquisitionClaudeStream()

        monkeypatch.setattr(pipeline.anthropic_client.messages, "stream", stream)


def _provider_request_count(pipeline, provider):
    client = {"openai": pipeline.client, "gemini": pipeline.gemini_client, "claude": pipeline.anthropic_client}[provider]
    return len(client.requests)


def _twilio_media_streams_pipeline(server, provider, client, lang="ar"):
    """A genuine (non-SmartPBX) Twilio Media Streams pipeline: no
    `_smartpbx_transfer_context`, so `_is_direct_smartpbx_english()` is False
    and none of the Phase B SmartPBX-only behaviour applies."""
    pipeline = server.MediaStreamSession(
        websocket=None, lang=lang, media_transport=FakeTransport(),
        anthropic_client=client if provider == "claude" else None,
        gemini_client=client if provider == "gemini" else None,
        openai_client=client if provider == "openai" else None,
        llm_provider=provider, model=f"{provider}-tool-model",
    )
    pipeline.tools = [{"provider": provider}]
    return pipeline


def _run_direct_provider(pipeline, provider):
    if provider == "openai":
        return pipeline._run_llm()
    if provider == "gemini":
        return pipeline._run_llm_gemini()
    return pipeline._run_llm_claude()


def _disable_initial_filler(monkeypatch, pipeline):
    """These tests call a runner directly (not `_process_utterance_bound`),
    so nothing ever runs the `finally` block that cancels an armed filler.
    Disabling it avoids leaking a pending filler task past the test."""
    monkeypatch.setattr(pipeline, "_start_initial_smartpbx_filler", lambda **_kwargs: None)


@pytest.mark.asyncio
@pytest.mark.parametrize("provider", ["claude", "gemini", "openai"])
async def test_smartpbx_empty_response_retries_once_then_succeeds(monkeypatch, provider):
    import server

    client = direct_tool_client(provider, [
        direct_empty_round(provider),
        direct_text_round(provider, "Recovered content."),
    ])
    pipeline = direct_tool_pipeline(server, provider, client)
    _disable_initial_filler(monkeypatch, pipeline)
    spoken = []

    async def speak(text, generation=-1):
        spoken.append(text)

    monkeypatch.setattr(pipeline, "_speak", speak)
    result = await _run_direct_provider(pipeline, provider)

    assert "Recovered content." in result
    assert _provider_request_count(pipeline, provider) == 2
    assert server.SMARTPBX_LLM_EMPTY_RETRY_RECOVERY_TEXT not in spoken
    assert server.SMARTPBX_LLM_TOOL_STARTED_RECOVERY_TEXT not in spoken


@pytest.mark.asyncio
@pytest.mark.parametrize("provider", ["claude", "gemini", "openai"])
async def test_smartpbx_empty_response_retries_once_then_speaks_recovery(monkeypatch, provider):
    import server

    client = direct_tool_client(provider, [
        direct_empty_round(provider),
        direct_empty_round(provider),
    ])
    pipeline = direct_tool_pipeline(server, provider, client)
    _disable_initial_filler(monkeypatch, pipeline)
    spoken = []

    async def speak(text, generation=-1):
        spoken.append(text)

    monkeypatch.setattr(pipeline, "_speak", speak)
    result = await _run_direct_provider(pipeline, provider)

    # Exactly one retry: two requests total, never a third.
    assert _provider_request_count(pipeline, provider) == 2
    assert server.SMARTPBX_LLM_EMPTY_RETRY_RECOVERY_TEXT in spoken
    assert server.SMARTPBX_LLM_TOOL_STARTED_RECOVERY_TEXT not in spoken
    assert server.SMARTPBX_LLM_EMPTY_RETRY_RECOVERY_TEXT in result


@pytest.mark.asyncio
@pytest.mark.parametrize("provider", ["claude", "gemini", "openai"])
async def test_smartpbx_empty_after_tool_start_never_replays_and_speaks_recovery(monkeypatch, provider):
    import server

    client = direct_tool_client(provider, [
        direct_tool_round(provider, {"safe": "value"}),
        direct_empty_round(provider),
    ])
    pipeline = direct_tool_pipeline(server, provider, client)
    _disable_initial_filler(monkeypatch, pipeline)
    spoken = []

    async def speak(text, generation=-1):
        spoken.append(text)

    async def successful_tool(_name, _arguments):
        return json.dumps({"status": "ok"})

    monkeypatch.setattr(server, "execute_tool", successful_tool)
    monkeypatch.setattr(pipeline, "_speak", speak)
    result = await _run_direct_provider(pipeline, provider)

    # Round 0 (the tool round) then round 1 (empty, post-tool): exactly two
    # requests, never a retry attempt against round 1 -- a retry there would
    # replay the provider turn against history that already committed a tool
    # result.
    assert _provider_request_count(pipeline, provider) == 2
    assert server.SMARTPBX_LLM_TOOL_STARTED_RECOVERY_TEXT in spoken
    assert server.SMARTPBX_LLM_EMPTY_RETRY_RECOVERY_TEXT not in spoken
    assert server.SMARTPBX_LLM_TOOL_STARTED_RECOVERY_TEXT in result


# ── Claude stream-outcome classification ────────────────────────────────
#
# Claude Sonnet 5 runs adaptive thinking by default, which the runner had
# never seen a fixture for, and the SDK reports WHY a turn ended only on
# `message_delta` -- which the runner had never read. Between them, a turn
# whose tool block was cut off by the output budget looked exactly like a
# turn that said nothing at all. These fixtures reproduce the real stream
# shapes so the five outcomes can be told apart.


def claude_thinking_events(thinking="Weighing the caller's dates."):
    """The thinking block Sonnet 5 emits before its visible output."""
    return [
        SimpleNamespace(
            type="content_block_start",
            content_block=SimpleNamespace(type="thinking"),
        ),
        SimpleNamespace(
            type="content_block_delta",
            delta=SimpleNamespace(type="thinking_delta", thinking=thinking),
        ),
        SimpleNamespace(type="content_block_stop"),
    ]


def claude_truncated_tool_round(tool_name="check_availability"):
    """The live failure: thinking, then a tool block whose JSON is cut off by
    the output budget. No `content_block_stop` ever arrives and the turn ends
    with `stop_reason=max_tokens`."""
    return claude_thinking_events() + [
        SimpleNamespace(
            type="content_block_start",
            content_block=SimpleNamespace(type="tool_use", id="tool-cut", name=tool_name),
        ),
        SimpleNamespace(
            type="content_block_delta",
            delta=SimpleNamespace(type="input_json_delta", partial_json='{"check_in": "2026-09-2'),
        ),
        claude_message_delta("max_tokens", 600),
    ]


def claude_incomplete_tool_round(tool_name="create_booking"):
    """An unterminated tool block with NO max_tokens stop -- a stream defect
    rather than a budget truncation, so it must classify differently."""
    return [
        SimpleNamespace(
            type="content_block_start",
            content_block=SimpleNamespace(type="tool_use", id="tool-partial", name=tool_name),
        ),
        SimpleNamespace(
            type="content_block_delta",
            delta=SimpleNamespace(type="input_json_delta", partial_json='{"guest_name": "Mi'),
        ),
    ]


def claude_malformed_tool_round(tool_name="create_booking"):
    """A tool block that DID terminate, carrying unparseable JSON."""
    return [
        SimpleNamespace(
            type="content_block_start",
            content_block=SimpleNamespace(type="tool_use", id="tool-bad", name=tool_name),
        ),
        SimpleNamespace(
            type="content_block_delta",
            delta=SimpleNamespace(type="input_json_delta", partial_json="{not json at all"),
        ),
        SimpleNamespace(type="content_block_stop"),
        claude_message_delta("tool_use", 180),
    ]


def _claude_outcome_pipeline(server, rounds, monkeypatch):
    client = direct_tool_client("claude", rounds)
    pipeline = direct_tool_pipeline(server, "claude", client)
    _disable_initial_filler(monkeypatch, pipeline)
    return client, pipeline


def _logged_round_outcomes(caplog):
    return [
        record.getMessage()
        for record in caplog.records
        if "event=llm_round_outcome" in record.getMessage()
    ]


def _forbidden_tool(*_args, **_kwargs):
    raise AssertionError("a discarded tool block must never be executed")


def _history_has_tool_use(pipeline):
    for message in pipeline.history:
        content = message.get("content")
        if isinstance(content, list) and any(
            isinstance(block, dict) and block.get("type") == "tool_use"
            for block in content
        ):
            return True
    return False


@pytest.mark.asyncio
async def test_claude_thinking_block_then_text_completes_and_is_spoken(monkeypatch, caplog):
    """(a) Adaptive thinking is enabled and must pass through untouched: the
    thinking block is neither spoken nor mistaken for content."""
    import server

    rounds = [claude_thinking_events() + direct_text_round("claude", "We have rooms free.")]
    client, pipeline = _claude_outcome_pipeline(server, rounds, monkeypatch)
    spoken = []

    async def speak(text, generation=-1):
        spoken.append(text)

    monkeypatch.setattr(pipeline, "_speak", speak)
    with caplog.at_level(logging.INFO):
        result = await pipeline._run_llm_claude()

    assert "We have rooms free." in result
    assert "We have rooms free." in spoken
    assert "Weighing the caller's dates." not in " ".join(spoken)
    assert len(client.requests) == 1
    outcomes = _logged_round_outcomes(caplog)
    assert len(outcomes) == 1
    assert "outcome=completed" in outcomes[0]


@pytest.mark.asyncio
async def test_claude_thinking_block_then_complete_tool_executes(monkeypatch, caplog):
    """(b) Thinking followed by a COMPLETE tool block still executes the tool."""
    import server

    rounds = [
        claude_thinking_events() + direct_tool_round("claude", {"safe": "value"}),
        direct_text_round("claude", "That is confirmed."),
    ]
    client, pipeline = _claude_outcome_pipeline(server, rounds, monkeypatch)
    executed = []

    async def speak(*_args, **_kwargs):
        return None

    async def record_tool(name, arguments):
        executed.append((name, arguments))
        return json.dumps({"status": "ok"})

    monkeypatch.setattr(server, "execute_tool", record_tool)
    monkeypatch.setattr(pipeline, "_speak", speak)
    with caplog.at_level(logging.INFO):
        result = await pipeline._run_llm_claude()

    assert [name for name, _ in executed] == ["create_booking"]
    assert "That is confirmed." in result
    assert len(client.requests) == 2
    outcomes = _logged_round_outcomes(caplog)
    assert len(outcomes) == 2
    assert all("outcome=completed" in line for line in outcomes)


@pytest.mark.asyncio
async def test_claude_max_tokens_truncation_is_classified_and_never_runs_the_tool(
    monkeypatch, caplog
):
    """(c) The live bug. A budget-truncated tool block is reported as
    truncation -- not as an empty turn -- retried exactly once, and then
    recovered from. The half-streamed tool must never run."""
    import server

    rounds = [claude_truncated_tool_round(), claude_truncated_tool_round()]
    client, pipeline = _claude_outcome_pipeline(server, rounds, monkeypatch)
    spoken = []

    async def speak(text, generation=-1):
        spoken.append(text)

    monkeypatch.setattr(server, "execute_tool", _forbidden_tool)
    monkeypatch.setattr(pipeline, "_speak", speak)
    with caplog.at_level(logging.INFO):
        result = await pipeline._run_llm_claude()

    outcomes = _logged_round_outcomes(caplog)
    assert len(outcomes) == 2
    assert all("outcome=max_tokens_truncated" in line for line in outcomes)
    assert "stop_reason=max_tokens" in outcomes[0]
    assert "output_tokens=600" in outcomes[0]
    assert "attempt=1" in outcomes[0] and "attempt=2" in outcomes[1]

    # Protection #7: exactly one retry, then the existing recovery line.
    assert len(client.requests) == 2
    assert server.SMARTPBX_LLM_EMPTY_RETRY_RECOVERY_TEXT in spoken
    assert server.SMARTPBX_LLM_EMPTY_RETRY_RECOVERY_TEXT in result
    assert not _history_has_tool_use(pipeline)


@pytest.mark.asyncio
async def test_claude_incomplete_tool_block_is_discarded_and_never_stored(monkeypatch, caplog):
    """(d) A tool block that never reached `content_block_stop` is dropped
    whole: not executed, not written to history, and reported as its own
    outcome rather than as a truncation."""
    import server

    rounds = [claude_incomplete_tool_round(), claude_incomplete_tool_round()]
    client, pipeline = _claude_outcome_pipeline(server, rounds, monkeypatch)
    spoken = []

    async def speak(text, generation=-1):
        spoken.append(text)

    monkeypatch.setattr(server, "execute_tool", _forbidden_tool)
    monkeypatch.setattr(pipeline, "_speak", speak)
    with caplog.at_level(logging.INFO):
        result = await pipeline._run_llm_claude()

    outcomes = _logged_round_outcomes(caplog)
    assert len(outcomes) == 2
    assert all("outcome=incomplete_tool_block" in line for line in outcomes)
    assert "stop_reason=none" in outcomes[0]

    assert len(client.requests) == 2
    assert server.SMARTPBX_LLM_EMPTY_RETRY_RECOVERY_TEXT in spoken
    assert server.SMARTPBX_LLM_EMPTY_RETRY_RECOVERY_TEXT in result
    assert not _history_has_tool_use(pipeline)


@pytest.mark.asyncio
async def test_claude_malformed_tool_json_is_discarded_and_never_stored(monkeypatch, caplog):
    """A terminated tool block with unparseable JSON is discarded too --
    executing it would invent arguments the model never sent."""
    import server

    rounds = [claude_malformed_tool_round(), claude_malformed_tool_round()]
    client, pipeline = _claude_outcome_pipeline(server, rounds, monkeypatch)
    spoken = []

    async def speak(text, generation=-1):
        spoken.append(text)

    monkeypatch.setattr(server, "execute_tool", _forbidden_tool)
    monkeypatch.setattr(pipeline, "_speak", speak)
    with caplog.at_level(logging.INFO):
        result = await pipeline._run_llm_claude()

    outcomes = _logged_round_outcomes(caplog)
    assert outcomes and all("outcome=malformed_tool_json" in line for line in outcomes)
    assert server.SMARTPBX_LLM_EMPTY_RETRY_RECOVERY_TEXT in result
    assert not _history_has_tool_use(pipeline)


@pytest.mark.asyncio
async def test_claude_true_empty_round_keeps_existing_retry_and_recovery(monkeypatch, caplog):
    """(e) A clean stop with no content at all is still `true_empty`, and its
    caller-facing handling is unchanged."""
    import server

    rounds = [
        [claude_message_delta("end_turn", 3)],
        [claude_message_delta("end_turn", 3)],
    ]
    client, pipeline = _claude_outcome_pipeline(server, rounds, monkeypatch)
    spoken = []

    async def speak(text, generation=-1):
        spoken.append(text)

    monkeypatch.setattr(pipeline, "_speak", speak)
    with caplog.at_level(logging.INFO):
        result = await pipeline._run_llm_claude()

    outcomes = _logged_round_outcomes(caplog)
    assert len(outcomes) == 2
    assert all("outcome=true_empty" in line for line in outcomes)
    assert "stop_reason=end_turn" in outcomes[0]

    assert len(client.requests) == 2
    assert server.SMARTPBX_LLM_EMPTY_RETRY_RECOVERY_TEXT in spoken
    assert server.SMARTPBX_LLM_EMPTY_RETRY_RECOVERY_TEXT in result


@pytest.mark.asyncio
async def test_claude_plain_text_round_behaviour_is_unchanged(monkeypatch, caplog):
    """(f) The ordinary case: one round, spoken, no retry, no recovery."""
    import server

    rounds = [direct_text_round("claude", "Certainly, Mr Fernando.")]
    client, pipeline = _claude_outcome_pipeline(server, rounds, monkeypatch)
    spoken = []

    async def speak(text, generation=-1):
        spoken.append(text)

    monkeypatch.setattr(pipeline, "_speak", speak)
    with caplog.at_level(logging.INFO):
        result = await pipeline._run_llm_claude()

    assert "Certainly, Mr Fernando." in result
    assert "Certainly, Mr Fernando." in spoken
    assert len(client.requests) == 1
    assert server.SMARTPBX_LLM_EMPTY_RETRY_RECOVERY_TEXT not in spoken
    outcomes = _logged_round_outcomes(caplog)
    assert len(outcomes) == 1
    assert "outcome=completed" in outcomes[0]
    # A round that COMPLETED necessarily reported why it ended -- that is now a
    # precondition of the outcome, not an incidental property of the fixture.
    assert "stop_reason=end_turn" in outcomes[0]


@pytest.mark.asyncio
async def test_claude_round_outcome_log_line_carries_no_caller_content(monkeypatch, caplog):
    """Spec §5: the outcome log is an enum, a stop reason, a token count and
    an attempt index -- never text, tool arguments or caller identifiers."""
    import server

    secret_json = '{"guest_name": "private-guest-sentinel'
    round_events = [
        SimpleNamespace(
            type="content_block_start",
            content_block=SimpleNamespace(type="tool_use", id="tool-x", name="create_booking"),
        ),
        SimpleNamespace(
            type="content_block_delta",
            delta=SimpleNamespace(type="input_json_delta", partial_json=secret_json),
        ),
        claude_message_delta("max_tokens", 600),
    ]
    client, pipeline = _claude_outcome_pipeline(
        server, [round_events, list(round_events)], monkeypatch
    )

    async def speak(*_args, **_kwargs):
        return None

    monkeypatch.setattr(server, "execute_tool", _forbidden_tool)
    monkeypatch.setattr(pipeline, "_speak", speak)
    with caplog.at_level(logging.INFO):
        await pipeline._run_llm_claude()

    for line in _logged_round_outcomes(caplog):
        assert "private-guest-sentinel" not in line
        assert "guest_name" not in line
        assert "create_booking" not in line


# ── Outcome-driven round control, TTS fencing, and stream shapes ─────────
#
# The runner used to decide "proceed or recover?" by re-inspecting whatever
# happened to be in `text_content`/`tool_use_blocks`, which meant a round that
# streamed a preamble sentence and THEN truncated its tool block looked
# "successful": the preamble was spoken, the surviving tool blocks were
# dispatched, and the truncation was only ever a log line. These fixtures pin
# the opposite contract -- the classified outcome alone decides, a non-completed
# round is discarded whole, and its in-flight audio is fenced before anything
# else speaks.

PREAMBLE_SENTENCE = "Let me check that for you."


def claude_thinking_only_round(*, stop_reason="end_turn", output_tokens=42, terminal=True):
    """A round that spends its whole budget inside the thinking block. With
    terminal metadata the model told us the turn ended; without it, the stream
    simply stopped arriving."""
    events = claude_thinking_events()
    if terminal:
        events.append(claude_message_delta(stop_reason, output_tokens))
        events.append(claude_message_stop())
    return events


def claude_preamble_then_truncated_tool_round(tool_name="check_availability"):
    """The mixed case §2 exists for: a spoken preamble is already streaming to
    the caller when the tool block runs out of budget mid-JSON."""
    return [
        SimpleNamespace(
            type="content_block_delta",
            delta=SimpleNamespace(type="text_delta", text=PREAMBLE_SENTENCE + " "),
        ),
        YIELD_TO_EVENT_LOOP,
        SimpleNamespace(
            type="content_block_start",
            content_block=SimpleNamespace(type="tool_use", id="tool-cut", name=tool_name),
        ),
        SimpleNamespace(
            type="content_block_delta",
            delta=SimpleNamespace(type="input_json_delta", partial_json='{"check_in": "2026-09-2'),
        ),
        claude_message_delta("max_tokens", 600),
        claude_message_stop(),
    ]


def claude_preamble_then_malformed_tool_round(tool_name="create_booking"):
    """Same shape, but the tool block DID terminate carrying unparseable JSON."""
    return [
        SimpleNamespace(
            type="content_block_delta",
            delta=SimpleNamespace(type="text_delta", text=PREAMBLE_SENTENCE + " "),
        ),
        YIELD_TO_EVENT_LOOP,
        SimpleNamespace(
            type="content_block_start",
            content_block=SimpleNamespace(type="tool_use", id="tool-bad", name=tool_name),
        ),
        SimpleNamespace(
            type="content_block_delta",
            delta=SimpleNamespace(type="input_json_delta", partial_json="{not json at all"),
        ),
        SimpleNamespace(type="content_block_stop"),
        claude_message_delta("tool_use", 180),
        claude_message_stop(),
    ]


def claude_complete_then_truncated_tool_round():
    """Two tool blocks in one round: the first complete and executable, the
    second cut off. Dispatching only the first would be a partial batch AND
    would make the retry a replay of a committed side effect."""
    return [
        SimpleNamespace(
            type="content_block_start",
            content_block=SimpleNamespace(
                type="tool_use", id="tool-complete", name="check_availability"
            ),
        ),
        SimpleNamespace(
            type="content_block_delta",
            delta=SimpleNamespace(
                type="input_json_delta", partial_json=json.dumps({"safe": "value"})
            ),
        ),
        SimpleNamespace(type="content_block_stop"),
        SimpleNamespace(
            type="content_block_start",
            content_block=SimpleNamespace(
                type="tool_use", id="tool-cut", name="create_booking"
            ),
        ),
        SimpleNamespace(
            type="content_block_delta",
            delta=SimpleNamespace(type="input_json_delta", partial_json='{"guest_name": "Mi'),
        ),
        claude_message_delta("max_tokens", 600),
        claude_message_stop(),
    ]


def _arm_delivery_tracking(pipeline):
    """Engage the ownership fences. A bare runner call leaves
    `_assistant_turn_generation` at -1, which makes
    `_smartpbx_fence_stalled_generation` correctly decline to touch a
    generation it does not own -- so without this the fencing under test is a
    no-op."""
    pipeline._start_assistant_turn_delivery_tracking()


class _GatedSpeak:
    """Records what started speaking and what actually finished. The preamble
    sentence blocks forever, so anything that completes it was NOT cancelled."""

    def __init__(self, blocking_text):
        self.blocking_text = blocking_text
        self.started = []
        self.completed = []
        self._never = asyncio.Event()

    async def __call__(self, text, generation=-1):
        self.started.append(text)
        if text == self.blocking_text:
            # Bounded on purpose. The fix cancels this task within the same
            # loop iteration, so the wait never elapses; a regression that
            # stops cancelling fails the assertions after two seconds instead
            # of wedging the whole suite on an un-cancelled gather.
            try:
                await asyncio.wait_for(self._never.wait(), timeout=2.0)
            except asyncio.TimeoutError:
                pass
        self.completed.append(text)


@pytest.mark.parametrize(
    ("stop_reason", "terminal", "expected"),
    [
        # (a) thinking burned the whole budget; the model reported max_tokens.
        ("max_tokens", True, "max_tokens_truncated"),
        # (b) the model finished cleanly having chosen to say nothing.
        ("end_turn", True, "true_empty"),
        # (c) abrupt EOF -- no message_delta, no message_stop, no evidence the
        # model ended anything. Must NOT be laundered into `true_empty`.
        (None, False, "stream_aborted"),
    ],
)
def test_claude_empty_round_shapes_are_told_apart_by_terminal_metadata(
    stop_reason, terminal, expected
):
    import server

    outcome = server._classify_claude_round_outcome(
        text_content="",
        tool_use_blocks=[],
        incomplete_tool_block=False,
        malformed_tool_json=False,
        stop_reason=stop_reason,
        saw_terminal_metadata=terminal,
    )

    assert outcome is server.SmartPBXClaudeRoundOutcome(expected)


def test_claude_stream_aborted_is_a_distinct_non_clean_outcome_that_never_proceeds():
    import server

    aborted = server.SmartPBXClaudeRoundOutcome.STREAM_ABORTED
    assert aborted is not server.SmartPBXClaudeRoundOutcome.TRUE_EMPTY
    assert aborted is not server.SmartPBXClaudeRoundOutcome.COMPLETED
    # It routes through the discard/fence path, not the clean-empty one.
    assert aborted in server.SMARTPBX_CLAUDE_DISCARD_ROUND_OUTCOMES
    assert (
        server.SmartPBXClaudeRoundOutcome.TRUE_EMPTY
        not in server.SMARTPBX_CLAUDE_DISCARD_ROUND_OUTCOMES
    )
    assert (
        server.SmartPBXClaudeRoundOutcome.COMPLETED
        not in server.SMARTPBX_CLAUDE_DISCARD_ROUND_OUTCOMES
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("stop_reason", "terminal", "expected"),
    [
        ("max_tokens", True, "max_tokens_truncated"),
        ("end_turn", True, "true_empty"),
        (None, False, "stream_aborted"),
    ],
)
async def test_claude_thinking_only_stream_shapes_log_distinctly_and_all_recover(
    monkeypatch, caplog, stop_reason, terminal, expected
):
    """All three empty shapes take the retry-once-then-recovery path, but each
    is logged as itself -- an aborted stream must be diagnosable as a transport
    fault, not filed under 'the model said nothing'."""
    import server

    rounds = [
        claude_thinking_only_round(stop_reason=stop_reason, terminal=terminal),
        claude_thinking_only_round(stop_reason=stop_reason, terminal=terminal),
    ]
    client, pipeline = _claude_outcome_pipeline(server, rounds, monkeypatch)
    spoken = []

    async def speak(text, generation=-1):
        spoken.append(text)

    monkeypatch.setattr(pipeline, "_speak", speak)
    with caplog.at_level(logging.INFO):
        result = await pipeline._run_llm_claude()

    outcomes = _logged_round_outcomes(caplog)
    assert len(outcomes) == 2
    assert all(f"outcome={expected}" in line for line in outcomes)
    assert len(client.requests) == 2
    assert server.SMARTPBX_LLM_EMPTY_RETRY_RECOVERY_TEXT in spoken
    assert server.SMARTPBX_LLM_EMPTY_RETRY_RECOVERY_TEXT in result


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("build_round", "expected_outcome"),
    [
        (claude_preamble_then_truncated_tool_round, "max_tokens_truncated"),
        (claude_preamble_then_malformed_tool_round, "malformed_tool_json"),
    ],
)
async def test_claude_preamble_with_bad_tool_block_runs_no_tool_and_fences_its_audio(
    monkeypatch, caplog, build_round, expected_outcome
):
    """§3 (a) and (b). The round produced visible text, so the old
    `text_content.strip() or tool_use_blocks` decision proceeded with it. The
    outcome now decides instead: no tool runs, the preamble's in-flight TTS is
    cancelled, the transport generation is cleared, and only the recovery line
    is ever delivered."""
    import server

    rounds = [build_round(), build_round()]
    client, pipeline = _claude_outcome_pipeline(server, rounds, monkeypatch)
    _arm_delivery_tracking(pipeline)
    transport = pipeline._media_transport
    speak = _GatedSpeak(PREAMBLE_SENTENCE)

    monkeypatch.setattr(server, "execute_tool", _forbidden_tool)
    monkeypatch.setattr(pipeline, "_speak", speak)
    with caplog.at_level(logging.INFO):
        result = await pipeline._run_llm_claude()

    outcomes = _logged_round_outcomes(caplog)
    assert len(outcomes) == 2
    assert all(f"outcome={expected_outcome}" in line for line in outcomes)

    # The preamble started streaming and was then cancelled mid-flight; it
    # never completed and it is not part of the turn's text.
    assert PREAMBLE_SENTENCE in speak.started
    assert PREAMBLE_SENTENCE not in speak.completed
    assert PREAMBLE_SENTENCE not in result
    # Undelivered audio was generation-cleared once per bad round.
    assert transport.clears >= 2
    assert pipeline._speak_generation >= 2

    # Exactly one retry, then the recovery line -- and no tool, ever.
    assert len(client.requests) == 2
    assert server.SMARTPBX_LLM_EMPTY_RETRY_RECOVERY_TEXT in speak.completed
    assert server.SMARTPBX_LLM_EMPTY_RETRY_RECOVERY_TEXT in result
    assert not _history_has_tool_use(pipeline)


@pytest.mark.asyncio
async def test_claude_complete_and_truncated_tool_in_one_round_executes_neither(
    monkeypatch, caplog
):
    """§3 (c). One round, two tool blocks, one of them cut off. Executing the
    complete one would be a half-dispatched batch and would make the retry a
    replay of a side effect that already happened. Zero executions is what
    makes retrying safe."""
    import server

    rounds = [
        claude_complete_then_truncated_tool_round(),
        claude_complete_then_truncated_tool_round(),
    ]
    client, pipeline = _claude_outcome_pipeline(server, rounds, monkeypatch)
    _arm_delivery_tracking(pipeline)
    executed = []
    spoken = []

    async def record_tool(name, arguments):
        executed.append((name, arguments))
        return json.dumps({"status": "ok"})

    async def speak(text, generation=-1):
        spoken.append(text)

    monkeypatch.setattr(server, "execute_tool", record_tool)
    monkeypatch.setattr(pipeline, "_speak", speak)
    with caplog.at_level(logging.INFO):
        result = await pipeline._run_llm_claude()

    assert executed == []
    assert not _history_has_tool_use(pipeline)

    outcomes = _logged_round_outcomes(caplog)
    assert len(outcomes) == 2
    assert all("outcome=max_tokens_truncated" in line for line in outcomes)
    assert len(client.requests) == 2
    assert server.SMARTPBX_LLM_EMPTY_RETRY_RECOVERY_TEXT in spoken
    assert server.SMARTPBX_LLM_EMPTY_RETRY_RECOVERY_TEXT in result


@pytest.mark.asyncio
async def test_claude_retry_after_a_discarded_round_can_still_complete_normally(
    monkeypatch
):
    """The discard/fence path must leave the turn usable: the retry that
    follows a truncated round speaks and executes normally."""
    import server

    rounds = [
        claude_preamble_then_truncated_tool_round(),
        direct_text_round("claude", "We have two suites free."),
    ]
    client, pipeline = _claude_outcome_pipeline(server, rounds, monkeypatch)
    _arm_delivery_tracking(pipeline)
    speak = _GatedSpeak(PREAMBLE_SENTENCE)

    monkeypatch.setattr(server, "execute_tool", _forbidden_tool)
    monkeypatch.setattr(pipeline, "_speak", speak)
    result = await pipeline._run_llm_claude()

    assert "We have two suites free." in result
    assert "We have two suites free." in speak.completed
    assert PREAMBLE_SENTENCE not in result
    assert PREAMBLE_SENTENCE not in speak.completed
    assert server.SMARTPBX_LLM_EMPTY_RETRY_RECOVERY_TEXT not in speak.started
    assert len(client.requests) == 2


# ── Terminal-failure precedence over visible output ──────────────────────
#
# The classifier used to ask "did this round produce anything?" BEFORE asking
# "did this round end badly?", so a round that streamed a sentence (or closed a
# tool block) and THEN hit the output budget -- or simply stopped arriving --
# classified as COMPLETED. The runner then did exactly what COMPLETED licenses:
# spoke the partial sentence, committed it to history, and dispatched the
# surviving tool blocks. Partial output from a cut-off round is wreckage, not an
# answer; these four shapes pin that it is now treated as such through the real
# runner path.


def claude_partial_text_round(*, terminal_stop_reason):
    """Text arm. A preamble sentence is already streaming to the caller when
    the round dies -- either the output budget ran out (`max_tokens`) or the
    stream stopped arriving (`terminal_stop_reason=None`: no `message_delta`,
    no `message_stop`). Either way the text is the prefix of a sentence the
    model never finished, and `tool_use_blocks` is empty, so the ONLY thing
    keeping this round out of the failure path used to be its visible text."""
    events = [
        SimpleNamespace(
            type="content_block_delta",
            delta=SimpleNamespace(type="text_delta", text=PREAMBLE_SENTENCE + " "),
        ),
        YIELD_TO_EVENT_LOOP,
    ]
    if terminal_stop_reason is not None:
        events.append(claude_message_delta(terminal_stop_reason, 600))
        events.append(claude_message_stop())
    return events


def claude_complete_tool_then_bad_end_round(*, terminal_stop_reason):
    """Tool arm. Same preamble, plus a tool block that reached
    `content_block_stop` carrying parseable JSON -- executable on its face,
    which is precisely why the old visible-output-first order dispatched it.
    The round still ended on the budget, or stopped arriving entirely, so we
    never saw what else it meant to emit: a batch whose end we did not witness
    may not be half-executed, and executing it would make the retry a replay."""
    events = [
        SimpleNamespace(
            type="content_block_delta",
            delta=SimpleNamespace(type="text_delta", text=PREAMBLE_SENTENCE + " "),
        ),
        YIELD_TO_EVENT_LOOP,
        SimpleNamespace(
            type="content_block_start",
            content_block=SimpleNamespace(
                type="tool_use", id="tool-complete", name="check_availability"
            ),
        ),
        SimpleNamespace(
            type="content_block_delta",
            delta=SimpleNamespace(
                type="input_json_delta",
                partial_json=json.dumps({"check_in": "2026-09-20", "check_out": "2026-09-22"}),
            ),
        ),
        SimpleNamespace(type="content_block_stop"),
    ]
    if terminal_stop_reason is not None:
        events.append(claude_message_delta(terminal_stop_reason, 600))
        events.append(claude_message_stop())
    return events


def _last_assistant_history_content(pipeline):
    for message in reversed(pipeline.history):
        if message.get("role") == "assistant":
            return message.get("content")
    return None


def _history_blob(pipeline):
    """Everything the turn committed, as one searchable string -- so a partial
    sentence cannot hide in a nested content block."""
    return json.dumps(pipeline.history, default=str)


def _retry_log_count(caplog):
    return len([
        record
        for record in caplog.records
        if "event=llm_empty_response" in record.getMessage()
        and "retrying=true" in record.getMessage()
    ])


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("build_round", "terminal_stop_reason", "expected_outcome"),
    [
        # (a) partial text, budget exhausted.
        (claude_partial_text_round, "max_tokens", "max_tokens_truncated"),
        # (b) partial text, abrupt EOF -- no terminal metadata at all.
        (claude_partial_text_round, None, "stream_aborted"),
        # (c) a COMPLETED tool block, budget exhausted.
        (claude_complete_tool_then_bad_end_round, "max_tokens", "max_tokens_truncated"),
        # (d) a COMPLETED tool block, abrupt EOF.
        (claude_complete_tool_then_bad_end_round, None, "stream_aborted"),
    ],
    ids=[
        "partial_text_max_tokens",
        "partial_text_abrupt_eof",
        "completed_tool_block_max_tokens",
        "completed_tool_block_abrupt_eof",
    ],
)
async def test_claude_terminal_failure_outranks_visible_output_in_the_runner(
    monkeypatch, caplog, build_round, terminal_stop_reason, expected_outcome
):
    """Terminal failure beats visible output, end to end through the runner.

    Each of the four shapes classified COMPLETED before the precedence fix and
    was therefore spoken, committed and (in the tool arm) dispatched. All four
    must now assert the same five things: the outcome is the terminal-failure
    one, no tool executes, the partial text never reaches history, the
    preamble's in-flight TTS is cancelled and generation-fenced, and exactly one
    retry then exactly one recovery utterance follows.
    """
    import server

    # Deliberately more rounds than the fix consumes. Exactly two are used once
    # the precedence is right (one attempt, one retry, then recovery); the spares
    # exist so that a REGRESSION -- which dispatches the tool block and loops for
    # another tool round -- fails on the `executed == []` assertion below instead
    # of on an `IndexError` from running the fixture dry.
    rounds = [
        build_round(terminal_stop_reason=terminal_stop_reason)
        for _ in range(server.MAX_TOOL_ROUNDS)
    ]
    client, pipeline = _claude_outcome_pipeline(server, rounds, monkeypatch)
    _arm_delivery_tracking(pipeline)
    transport = pipeline._media_transport
    executed = []
    speak = _GatedSpeak(PREAMBLE_SENTENCE)

    async def record_tool(name, arguments):
        executed.append((name, arguments))
        return json.dumps({"status": "ok"})

    # Recorded rather than raised: the runner catches Exception around tool
    # execution, so a raising stub would be swallowed into a tool_error and the
    # test would pass while the tool had in fact run.
    monkeypatch.setattr(server, "execute_tool", record_tool)
    monkeypatch.setattr(pipeline, "_speak", speak)
    with caplog.at_level(logging.INFO):
        result = await pipeline._run_llm_claude()

    # 1. Classified as the terminal failure, not as a completed round.
    outcomes = _logged_round_outcomes(caplog)
    assert len(outcomes) == 2
    assert all(f"outcome={expected_outcome}" in line for line in outcomes)

    # 2. Nothing executed. Not the complete tool block, not anything.
    assert executed == []
    assert not _history_has_tool_use(pipeline)

    # 3. The partial text was never committed. The only assistant entry the
    #    turn may leave behind is the recovery line.
    assert PREAMBLE_SENTENCE not in _history_blob(pipeline)
    assert _last_assistant_history_content(pipeline) != PREAMBLE_SENTENCE
    assert (
        _last_assistant_history_content(pipeline)
        == server.SMARTPBX_LLM_EMPTY_RETRY_RECOVERY_TEXT
    )
    assert PREAMBLE_SENTENCE not in result

    # 4. The in-flight TTS was fenced: the preamble started, was cancelled
    #    before it could finish, and the transport was generation-cleared once
    #    per discarded round. Nothing stale is delivered after recovery starts --
    #    the recovery line is the last thing that completes.
    assert speak.started.count(PREAMBLE_SENTENCE) == 2
    assert PREAMBLE_SENTENCE not in speak.completed
    assert transport.clears >= 2
    assert pipeline._speak_generation >= 2
    assert speak.completed == [server.SMARTPBX_LLM_EMPTY_RETRY_RECOVERY_TEXT]

    # 5. Exactly one retry, exactly one recovery utterance.
    assert len(client.requests) == 2
    assert _retry_log_count(caplog) == 1
    assert speak.started.count(server.SMARTPBX_LLM_EMPTY_RETRY_RECOVERY_TEXT) == 1
    assert result.count(server.SMARTPBX_LLM_EMPTY_RETRY_RECOVERY_TEXT) == 1


def test_claude_discard_set_is_every_non_completed_outcome_that_can_carry_output():
    """The discard/fence path must catch every outcome the precedence order can
    now produce with partial output attached. TRUE_EMPTY is the sole exclusion
    and it is structural, not a judgement call: it is only reachable once
    max_tokens, an unterminated block, malformed JSON and a missing terminal
    metadata have all been ruled out AND there is no text and no tool block --
    so there is nothing to discard and no TTS task to fence."""
    import server

    outcomes = set(server.SmartPBXClaudeRoundOutcome)
    assert server.SMARTPBX_CLAUDE_DISCARD_ROUND_OUTCOMES == outcomes - {
        server.SmartPBXClaudeRoundOutcome.COMPLETED,
        server.SmartPBXClaudeRoundOutcome.TRUE_EMPTY,
    }
    assert server._classify_claude_round_outcome(
        text_content="",
        tool_use_blocks=[],
        incomplete_tool_block=False,
        malformed_tool_json=False,
        stop_reason="end_turn",
        saw_terminal_metadata=True,
    ) is server.SmartPBXClaudeRoundOutcome.TRUE_EMPTY


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("end_turn", "end_turn"),
        ("max_tokens", "max_tokens"),
        ("tool_use", "tool_use"),
        ("stop_sequence", "stop_sequence"),
        ("refusal", "refusal"),
        (None, "none"),
        ("", "none"),
        ("some_future_reason", "unknown"),
        ("Mr Fernando, 0771234567", "unknown"),
        (17, "unknown"),
    ],
)
def test_claude_stop_reason_is_normalized_to_a_fixed_enum_before_logging(raw, expected):
    """A raw stop_reason is provider-controlled text. Only the fixed vocabulary
    (plus the absent/unknown buckets) may ever reach a log line."""
    import server

    assert server._normalized_claude_stop_reason(raw) == expected


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (600, "600"),
        (0, "0"),
        (-5, "0"),
        (10**12, str(1_000_000)),
        (None, "unknown"),
        ("600", "unknown"),
        (True, "unknown"),
    ],
)
def test_claude_logged_output_tokens_are_bounded(raw, expected):
    import server

    assert server._bounded_claude_output_tokens(raw) == expected


@pytest.mark.parametrize(
    ("raw", "expected"),
    [(1, 1), (2, 2), (0, 1), (-3, 1), (10**9, 9), (None, 1), ("2", 1)],
)
def test_claude_logged_attempt_number_is_bounded(raw, expected):
    import server

    assert server._bounded_claude_attempt(raw) == expected


@pytest.mark.asyncio
async def test_claude_round_outcome_never_logs_an_arbitrary_stop_reason_string(
    monkeypatch, caplog
):
    """End to end: an unrecognised stop_reason (which a proxy or a future API
    version can return verbatim) is bucketed, never echoed."""
    import server

    exotic = "guest_said_0771234567"
    rounds = [
        claude_thinking_events() + [claude_message_delta(exotic, 4), claude_message_stop()],
        claude_thinking_events() + [claude_message_delta(exotic, 4), claude_message_stop()],
    ]
    client, pipeline = _claude_outcome_pipeline(server, rounds, monkeypatch)

    async def speak(*_args, **_kwargs):
        return None

    monkeypatch.setattr(pipeline, "_speak", speak)
    with caplog.at_level(logging.INFO):
        await pipeline._run_llm_claude()

    outcomes = _logged_round_outcomes(caplog)
    assert outcomes
    for line in outcomes:
        assert exotic not in line
        assert "0771234567" not in line
        assert "stop_reason=unknown" in line


@pytest.mark.asyncio
@pytest.mark.parametrize("provider", ["claude", "gemini", "openai"])
async def test_smartpbx_initial_response_timeout_speaks_recovery_and_keeps_call_open(monkeypatch, provider):
    import server

    monkeypatch.setattr(server, "SMARTPBX_LLM_INITIAL_RESPONSE_TIMEOUT_SECONDS", 0.02)
    monkeypatch.setattr(server, "SMARTPBX_LLM_STALL_TIMEOUT_SECONDS", 0.02)
    client = direct_tool_client(provider, [])
    pipeline = direct_tool_pipeline(server, provider, client)
    _disable_initial_filler(monkeypatch, pipeline)
    _patch_direct_hanging_stream(monkeypatch, pipeline, provider, before_events=[])
    spoken = []

    async def speak(text, generation=-1):
        spoken.append(text)

    monkeypatch.setattr(pipeline, "_speak", speak)
    result = await _run_direct_provider(pipeline, provider)

    assert server.SMARTPBX_LLM_EMPTY_RETRY_RECOVERY_TEXT in spoken
    assert server.SMARTPBX_LLM_EMPTY_RETRY_RECOVERY_TEXT in result
    # The call stays open: _run_llm*/_run_llm_gemini/_run_llm_claude returned
    # normally (a string), it did not raise or close anything.
    assert isinstance(result, str)


@pytest.mark.asyncio
@pytest.mark.parametrize("provider", ["claude", "gemini", "openai"])
async def test_smartpbx_hanging_stream_acquisition_speaks_recovery_and_keeps_call_open(
    monkeypatch, provider
):
    """A client call that never returns (the stream is never even created/
    entered) must recover on the same initial-response deadline as a
    hanging first delta — item 1 of the PR-266 review round.

    Distinct from test_smartpbx_initial_response_timeout_speaks_recovery_
    and_keeps_call_open above: that test hangs AFTER acquisition returns
    (inside the already-guarded iteration). This one hangs INSIDE
    acquisition itself (the awaited create()/generate_content_stream() call,
    or Claude's ``async with ... __aenter__``), which was previously
    unguarded and would have blocked forever.
    """
    import server

    monkeypatch.setattr(server, "SMARTPBX_LLM_INITIAL_RESPONSE_TIMEOUT_SECONDS", 0.02)
    monkeypatch.setattr(server, "SMARTPBX_LLM_STALL_TIMEOUT_SECONDS", 0.02)
    client = direct_tool_client(provider, [])
    pipeline = direct_tool_pipeline(server, provider, client)
    _disable_initial_filler(monkeypatch, pipeline)
    _patch_direct_hanging_acquisition(monkeypatch, pipeline, provider)
    spoken = []

    async def speak(text, generation=-1):
        spoken.append(text)

    monkeypatch.setattr(pipeline, "_speak", speak)
    # Bounded wait_for around the call itself: if the acquisition guard
    # regresses, this fails fast with a clear TimeoutError instead of
    # hanging the test suite.
    result = await asyncio.wait_for(_run_direct_provider(pipeline, provider), timeout=5.0)

    assert server.SMARTPBX_LLM_EMPTY_RETRY_RECOVERY_TEXT in spoken
    assert server.SMARTPBX_LLM_EMPTY_RETRY_RECOVERY_TEXT in result
    assert isinstance(result, str)
    # A stream timeout (unlike an empty response) returns straight to
    # recovery without a second attempt — exactly one acquisition call.
    assert _provider_request_count(pipeline, provider) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("provider", ["claude", "gemini", "openai"])
async def test_smartpbx_stream_stall_timeout_speaks_recovery_after_partial_content(monkeypatch, provider):
    import server

    monkeypatch.setattr(server, "SMARTPBX_LLM_INITIAL_RESPONSE_TIMEOUT_SECONDS", 5.0)
    monkeypatch.setattr(server, "SMARTPBX_LLM_STALL_TIMEOUT_SECONDS", 0.02)
    client = direct_tool_client(provider, [])
    pipeline = direct_tool_pipeline(server, provider, client)
    _disable_initial_filler(monkeypatch, pipeline)
    partial_event = direct_text_round(provider, "Partial before the stall.")[0]
    _patch_direct_hanging_stream(
        monkeypatch, pipeline, provider, before_events=[partial_event]
    )
    spoken = []

    async def speak(text, generation=-1):
        spoken.append(text)

    monkeypatch.setattr(pipeline, "_speak", speak)
    result = await _run_direct_provider(pipeline, provider)

    # Pre-tool: the stall counts as an empty-ish failure (no tool started),
    # so it takes the retry-exhausted line, not the post-tool line.
    assert server.SMARTPBX_LLM_EMPTY_RETRY_RECOVERY_TEXT in spoken


@pytest.mark.asyncio
@pytest.mark.parametrize("provider", ["claude", "gemini", "openai"])
async def test_smartpbx_stall_recovery_cancels_delayed_tts_before_speaking_recovery(
    monkeypatch, provider
):
    """Item 3 (PR-266 review round 2): before speaking the recovery line, a
    stall must cancel-and-await this generation's pending per-sentence TTS
    tasks — a TTS task whose completion is artificially delayed past the
    stall trigger must never deliver audio after/over the recovery line.

    ``never_release`` is deliberately never set, so the partial sentence's
    TTS call can only ever finish via cancellation. ``started`` proves the
    task actually ran (so the rest of the assertions aren't vacuous);
    ``cancelled`` proves it was actually cancelled rather than merely
    outlived by an early-returning round (the pre-fix bug: recovery speaks
    immediately while this task is simply left running, orphaned, still able
    to deliver its audio later -- including after/over the recovery line).
    """
    import server

    monkeypatch.setattr(server, "SMARTPBX_LLM_INITIAL_RESPONSE_TIMEOUT_SECONDS", 5.0)
    monkeypatch.setattr(server, "SMARTPBX_LLM_STALL_TIMEOUT_SECONDS", 0.02)
    client = direct_tool_client(provider, [])
    pipeline = direct_tool_pipeline(server, provider, client)
    _disable_initial_filler(monkeypatch, pipeline)
    # Trailing space after the period: _extract_sentences only emits a
    # complete sentence (and therefore spawns its per-sentence TTS task) on
    # a punctuation-then-whitespace boundary -- without it the text just
    # sits unflushed in the local sentence_buffer and no task is ever
    # created, which would make this test vacuously pass either way.
    partial_event = direct_text_round(provider, "Partial before the stall. ")[0]
    _patch_direct_hanging_stream(
        monkeypatch, pipeline, provider, before_events=[partial_event]
    )
    spoken = []
    started = []
    cancelled = []
    never_release = asyncio.Event()

    async def speak(text, generation=-1):
        if text == "Partial before the stall.":
            started.append(text)
            try:
                await never_release.wait()
            except asyncio.CancelledError:
                cancelled.append(text)
                raise
        spoken.append(text)

    monkeypatch.setattr(pipeline, "_speak", speak)

    result = await asyncio.wait_for(_run_direct_provider(pipeline, provider), timeout=5.0)

    # Sanity: the partial sentence's TTS task really started (otherwise the
    # assertions below would hold vacuously in both old and new code).
    assert started == ["Partial before the stall."]
    # It was cancelled -- not merely outlived by an early return.
    assert cancelled == ["Partial before the stall."]
    # Never delivered -- cancelled before it could complete.
    assert "Partial before the stall." not in spoken
    assert server.SMARTPBX_LLM_EMPTY_RETRY_RECOVERY_TEXT in spoken
    assert server.SMARTPBX_LLM_EMPTY_RETRY_RECOVERY_TEXT in result


@pytest.mark.asyncio
@pytest.mark.parametrize("provider", ["claude", "gemini", "openai"])
async def test_smartpbx_stall_timeout_recovery_is_atomic_against_locked_filler(
    monkeypatch, provider
):
    """Regression test for the atomic timeout-recovery transition (PR-266
    follow-up fix): on a stream stall, recovery must cancel-and-await the
    stalled generation's initial filler BEFORE the recovery line ever tries
    to speak.

    Enters through the REAL ``_process_utterance_bound`` (not a runner
    called directly), with the real filler machinery running end to end --
    only the filler delay is shrunk via monkeypatch, the controller class
    and its cancel/await machinery are untouched. The fake TTS BLOCKS while
    holding the real ``_speak_lock``, simulating in-flight filler speech at
    the exact moment the (also faked) provider stream stalls.

    Pre-fix, ``_smartpbx_handle_stream_timeout`` never touched the filler at
    all: it would still be sitting inside ``_speak``'s ``async with
    self._speak_lock:`` block, blocked in TTS, when the recovery line tried
    to acquire that same lock -- a deadlock. The whole scenario is bounded
    by ``asyncio.wait_for`` so a regression here fails with a clear timeout
    instead of hanging the suite.
    """
    import server

    monkeypatch.setattr(server, "retrieve_context", lambda _text: "")
    monkeypatch.setattr(server, "SMARTPBX_INITIAL_FILLER_DELAY_SECONDS", 0.01)
    monkeypatch.setattr(server, "SMARTPBX_LLM_INITIAL_RESPONSE_TIMEOUT_SECONDS", 0.2)
    monkeypatch.setattr(server, "SMARTPBX_LLM_STALL_TIMEOUT_SECONDS", 0.2)

    client = direct_tool_client(provider, [])
    pipeline = direct_tool_pipeline(server, provider, client)
    # The provider stream is acquired but never yields anything -- the
    # initial-response deadline is what ends the round, not real content.
    _patch_direct_hanging_stream(monkeypatch, pipeline, provider, before_events=[])

    turn_summaries = []

    # Same shape as the production sink `_emit_smartpbx_turn_telemetry`: the
    # event name arrives POSITIONALLY and is ALSO repeated inside **fields, so
    # naming this parameter `event` makes every emit raise TypeError.
    def fake_emit(_event, **fields):
        if _event == "turn_summary":
            turn_summaries.append(fields)

    # Mirrors exactly what `_flush_transcript` does before dispatching a
    # turn, so `_process_utterance_bound` sees a real, telemetry-owned turn.
    pipeline._turn_telemetry = server.SmartPBXTurnTelemetry(
        emit=fake_emit, session_trace_id="test-trace",
    )
    pipeline._active_smartpbx_turn_id = pipeline._turn_telemetry.start_turn("final")

    filler_started = asyncio.Event()
    filler_release = asyncio.Event()  # never set: only cancellation ends the filler
    filler_cancelled = []
    delivered = []

    async def fake_tts_elevenlabs(text, *, sentence=None, turn_generation=None):
        if text == server.SMARTPBX_INITIAL_FILLER_TEXT:
            filler_started.set()
            try:
                await filler_release.wait()
            except asyncio.CancelledError:
                filler_cancelled.append(text)
                raise
            return
        delivered.append(text)

    # Only the innermost TTS call is faked -- `_speak`'s real `_speak_lock`
    # acquisition and generation-fence checks stay exactly as in production.
    monkeypatch.setattr(pipeline, "_tts_elevenlabs", fake_tts_elevenlabs)

    turn_task = asyncio.create_task(
        pipeline._process_utterance_bound("guest asks a slow question")
    )
    # Confirm the filler really is mid-flight and holding _speak_lock before
    # letting the stall timeout run its course -- otherwise the deadlock
    # this test exists to catch would never actually be exercised.
    await asyncio.wait_for(filler_started.wait(), timeout=2.0)
    assert pipeline._speak_lock.locked()

    await asyncio.wait_for(turn_task, timeout=5.0)

    # Filler cancellation completed -- not merely requested.
    assert filler_cancelled == [server.SMARTPBX_INITIAL_FILLER_TEXT]
    controller = pipeline._smartpbx_initial_filler
    assert controller is None or controller._task.done()

    # Exactly one recovery sentence spoken, and no stale filler audio ever
    # lands after (or instead of) it.
    assert delivered == [server.SMARTPBX_LLM_EMPTY_RETRY_RECOVERY_TEXT]
    assert server.SMARTPBX_INITIAL_FILLER_TEXT not in delivered

    # The turn summarizes exactly once.
    assert len(turn_summaries) == 1

    # No tasks left running behind the turn (filler task, tts tasks, ...).
    await asyncio.sleep(0)
    leftover = [
        t for t in asyncio.all_tasks()
        if t is not asyncio.current_task() and not t.done()
    ]
    assert leftover == []


@pytest.mark.asyncio
async def test_smartpbx_stream_timeout_after_tool_start_speaks_post_tool_recovery(monkeypatch):
    import server

    provider = "openai"
    monkeypatch.setattr(server, "SMARTPBX_LLM_INITIAL_RESPONSE_TIMEOUT_SECONDS", 0.02)
    monkeypatch.setattr(server, "SMARTPBX_LLM_STALL_TIMEOUT_SECONDS", 0.02)
    client = direct_tool_client(provider, [direct_tool_round(provider, {"safe": "value"})])
    pipeline = direct_tool_pipeline(server, provider, client)
    _disable_initial_filler(monkeypatch, pipeline)
    spoken = []

    async def speak(text, generation=-1):
        spoken.append(text)

    async def successful_tool(_name, _arguments):
        return json.dumps({"status": "ok"})

    monkeypatch.setattr(server, "execute_tool", successful_tool)
    monkeypatch.setattr(pipeline, "_speak", speak)

    original_create = pipeline.client.create
    call_count = 0

    async def create(**kwargs):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return await original_create(**kwargs)

        async def hang():
            await asyncio.Event().wait()
            yield None

        return hang()

    monkeypatch.setattr(pipeline.client, "create", create)
    result = await pipeline._run_llm()

    assert server.SMARTPBX_LLM_TOOL_STARTED_RECOVERY_TEXT in spoken
    assert server.SMARTPBX_LLM_EMPTY_RETRY_RECOVERY_TEXT not in spoken
    assert result.endswith(server.SMARTPBX_LLM_TOOL_STARTED_RECOVERY_TEXT)


@pytest.mark.asyncio
async def test_smartpbx_barge_in_during_empty_retry_prevents_stale_recovery_speech(monkeypatch):
    """A barge-in that lands between the empty round and the retry's recovery
    speech must leave no stale audio/history behind -- `_invoke_speak` and
    `_append_assistant_history` both revalidate ownership, so the abandoned
    runner's recovery line is silently dropped instead of spoken over the
    caller's new turn."""
    import server

    provider = "openai"
    client = direct_tool_client(provider, [
        direct_empty_round(provider),
        direct_empty_round(provider),
    ])
    pipeline = direct_tool_pipeline(server, provider, client)
    _disable_initial_filler(monkeypatch, pipeline)
    old_turn, new_turn = "turn-old", "turn-new"
    pipeline._active_smartpbx_turn_id = old_turn
    spoken = []

    async def speak(text, generation=-1):
        spoken.append(text)
        # Simulate a genuine barge-in landing mid-retry: a newer turn takes
        # ownership before this stale runner's recovery line goes out.
        pipeline._active_smartpbx_turn_id = new_turn
        pipeline._speak_generation += 1

    monkeypatch.setattr(pipeline, "_speak", speak)

    runner = server._SmartPBXRunnerContext(
        turn_id=old_turn,
        dropped_frame_baseline=0,
        speak_generation=pipeline._speak_generation,
        raw_utterance="",
    )
    token = server._smartpbx_runner_context.set(runner)
    try:
        result = await pipeline._run_llm()
    finally:
        server._smartpbx_runner_context.reset(token)

    assert result == ""
    assert not any(
        message.get("content") == server.SMARTPBX_LLM_EMPTY_RETRY_RECOVERY_TEXT
        for message in pipeline.history
        if isinstance(message, dict)
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("provider", ["claude", "gemini", "openai"])
async def test_twilio_media_streams_empty_response_behaviour_is_unchanged(monkeypatch, provider):
    """Non-SmartPBX Twilio Media Streams (Arabic/Sinhala/Tamil) must not gain
    the NEW SmartPBX-only retry-nudge/recovery-line policy -- Phase B's
    shared empty-response policy and stream timeouts apply only to the
    direct SmartPBX English path.

    Gemini is a deliberate exception to "no retry at all": its own
    per-turn empty-response retry (``GEMINI_EMPTY_RETRY_NUDGE`` + the
    canned ``LLM_EMPTY_FALLBACKS`` line) predates Phase B entirely
    (commit 716ec9e) and is unconditional -- it fires for every session,
    not just direct SmartPBX -- so it needs a second empty round to
    attempt against and is "unchanged" at 2 requests with the old canned
    fallback spoken, not silence. OpenAI/Claude have no such per-provider
    retry outside the SmartPBX-gated Phase B policy and stay silent at
    one request, as before.
    """
    import server

    rounds = [direct_empty_round(provider)]
    if provider == "gemini":
        rounds.append(direct_empty_round(provider))
    client = direct_tool_client(provider, rounds)
    pipeline = _twilio_media_streams_pipeline(server, provider, client, lang="ar")
    spoken = []

    async def speak(text, generation=-1):
        spoken.append(text)

    monkeypatch.setattr(pipeline, "_speak", speak)
    result = await _run_direct_provider(pipeline, provider)

    assert server.SMARTPBX_LLM_EMPTY_RETRY_RECOVERY_TEXT not in spoken
    assert server.SMARTPBX_LLM_TOOL_STARTED_RECOVERY_TEXT not in spoken
    if provider == "gemini":
        fallback = server.LLM_EMPTY_FALLBACKS["ar"]
        assert _provider_request_count(pipeline, provider) == 2
        assert spoken == [fallback]
        assert result == fallback
    else:
        assert _provider_request_count(pipeline, provider) == 1
        assert spoken == []
        assert result == ""


def test_smartpbx_initial_filler_delay_default_is_1_5_seconds():
    import server

    assert server.SMARTPBX_INITIAL_FILLER_DELAY_SECONDS == 1.5
    assert server._resolve_smartpbx_initial_filler_delay(None) == 1.5
    assert server._resolve_smartpbx_initial_filler_delay("") == 1.5


@pytest.mark.asyncio
@pytest.mark.parametrize("provider", ["claude", "gemini", "openai"])
async def test_spoken_initial_filler_drains_before_real_answer_delivery_and_history(
    monkeypatch, provider
):
    """A normal answer follows the delivered filler without a media clear.

    Drive the real provider runner, filler controller, speak lock, generation
    fence, delivery acknowledgement, and history writer.  The clock is
    controlled only so CI need not spend 1.5 wall-clock seconds proving that
    the production default won the race before the first provider content.
    """
    import server

    answer = "I have your dates."
    content_release = asyncio.Event()

    async def delayed_content():
        await content_release.wait()
        for event in direct_text_round(provider, answer):
            yield event

    client = controlled_direct_tool_client(provider, [delayed_content()])
    pipeline = direct_tool_pipeline(server, provider, client)
    transport = GenerationTrackingTransport()
    pipeline._media_transport = transport
    filler_sleep = _ControlledInitialFillerSleep()
    filler_spoken = asyncio.Event()
    spoken = []
    real_controller = server.SmartPBXInitialFillerController

    def controlled_controller(**kwargs):
        return real_controller(**kwargs, sleep=filler_sleep)

    async def tts(text, *, sentence=None, turn_generation=None):
        spoken.append((text, sentence, turn_generation))
        pipeline._is_speaking = True
        await pipeline._send_tts_done(
            sentence=sentence, turn_generation=turn_generation,
        )
        if text == server.SMARTPBX_INITIAL_FILLER_TEXT:
            filler_spoken.set()

    monkeypatch.setattr(server, "SmartPBXInitialFillerController", controlled_controller)
    monkeypatch.setattr(server, "retrieve_context", lambda _text: "")
    monkeypatch.setattr(pipeline, "_tts_elevenlabs", tts)

    turn = asyncio.create_task(
        pipeline._process_utterance_bound("guest supplied booking dates")
    )
    try:
        await asyncio.wait_for(filler_sleep.entered.wait(), timeout=1)
        assert filler_sleep.delays == [1.5]
        assert spoken == []

        filler_sleep.release.set()
        await asyncio.wait_for(filler_spoken.wait(), timeout=1)
        assert spoken == [(server.SMARTPBX_INITIAL_FILLER_TEXT, None, 0)]

        content_release.set()
        await asyncio.wait_for(turn, timeout=2)

        assert transport.clears == 0
        assert pipeline._speak_generation == transport.generation == 0
        assert pipeline._smartpbx_barge_ins == 0
        assert pipeline._assistant_turn_generation == pipeline._speak_generation
        assert pipeline._assistant_turn_generated_sentences == [answer]
        assert pipeline._delivered_sentences == [answer]
        assert pipeline.history[-1] == {"role": "assistant", "content": answer}
        assert pipeline.full_transcript[-1] == {"role": "assistant", "text": answer}
        assert pipeline._assistant_turn_was_interrupted() is False
    finally:
        content_release.set()
        filler_sleep.release.set()
        await asyncio.gather(turn, return_exceptions=True)
        pipeline._cancel_reprompt()


@pytest.mark.asyncio
async def test_filler_handoff_cannot_rebind_a_barged_out_runner_to_the_new_turn(
    monkeypatch,
):
    """A delivery-generation handoff must never restore stale turn ownership."""
    import server

    answer = "This old answer must stay fenced."
    content_release = asyncio.Event()

    async def delayed_content():
        await content_release.wait()
        for event in direct_text_round("claude", answer):
            yield event

    client = controlled_direct_tool_client("claude", [delayed_content()])
    pipeline = direct_tool_pipeline(server, "claude", client)
    transport = GenerationTrackingTransport()
    pipeline._media_transport = transport
    pipeline._active_smartpbx_turn_id = "old-turn"
    filler_sleep = _ControlledInitialFillerSleep()
    filler_spoken = asyncio.Event()
    spoken = []
    real_controller = server.SmartPBXInitialFillerController

    def controlled_controller(**kwargs):
        return real_controller(**kwargs, sleep=filler_sleep)

    async def tts(text, *, sentence=None, turn_generation=None):
        spoken.append(text)
        pipeline._is_speaking = True
        await pipeline._send_tts_done(
            sentence=sentence, turn_generation=turn_generation,
        )
        if text == server.SMARTPBX_INITIAL_FILLER_TEXT:
            filler_spoken.set()

    monkeypatch.setattr(server, "SmartPBXInitialFillerController", controlled_controller)
    monkeypatch.setattr(server, "retrieve_context", lambda _text: "")
    monkeypatch.setattr(pipeline, "_tts_elevenlabs", tts)

    turn = asyncio.create_task(pipeline._process_utterance_bound("old guest turn"))
    try:
        await asyncio.wait_for(filler_sleep.entered.wait(), timeout=1)
        filler_sleep.release.set()
        await asyncio.wait_for(filler_spoken.wait(), timeout=1)

        await pipeline._handle_bargein()
        pipeline._active_smartpbx_turn_id = "new-turn"
        history_after_barge = list(pipeline.history)
        content_release.set()
        await asyncio.wait_for(turn, timeout=2)

        assert pipeline._speak_generation == transport.generation == 1
        assert pipeline._smartpbx_barge_ins == 1
        assert spoken == [server.SMARTPBX_INITIAL_FILLER_TEXT]
        assert pipeline.history == history_after_barge
        assert all(
            message.get("content") != answer
            for message in pipeline.history
            if isinstance(message, dict)
        )
    finally:
        content_release.set()
        filler_sleep.release.set()
        await asyncio.gather(turn, return_exceptions=True)
        pipeline._cancel_reprompt()


@pytest.mark.asyncio
async def test_genuine_barge_in_still_records_only_the_delivered_prefix():
    import server

    pipeline = server.MediaStreamSession(
        websocket=None, lang="en", media_transport=GenerationTrackingTransport(),
    )
    pipeline._smartpbx_transfer_context = object()
    pipeline._active_smartpbx_turn_id = "interrupted-turn"
    pipeline._start_assistant_turn_delivery_tracking()
    generation = pipeline._assistant_turn_generation
    first = "Your check-in date is recorded."
    second = "Your check-out date is next."

    pipeline._record_generated_sentence(first)
    await pipeline._send_tts_done(sentence=first, turn_generation=generation)
    pipeline._record_generated_sentence(second)
    pipeline._is_speaking = True
    await pipeline._handle_bargein()
    pipeline._append_assistant_history({
        "role": "assistant",
        "content": f"{first} {second}",
    })

    assert pipeline._smartpbx_barge_ins == 1
    assert pipeline._assistant_turn_was_interrupted() is True
    assert pipeline._delivered_sentences == [first]
    assert pipeline.history[-1] == {
        "role": "assistant",
        "content": f"{first} [interrupted]",
    }
    pipeline._cancel_reprompt()


@pytest.mark.asyncio
async def test_timeout_recovery_fence_records_the_recovery_line_it_actually_spoke(
    monkeypatch,
):
    """The stall fence is the filler clear's twin — same bump, same re-anchor.

    ``_smartpbx_fence_stalled_generation`` bumps ``_speak_generation`` and
    clears the transport before the recovery line is spoken, exactly as the
    initial filler's ``clear_audio`` does.  Without re-anchoring the delivery
    tracker across that bump, the recovery sentence the caller genuinely
    heard is never acknowledged as delivered and the turn is written to
    history as ``[interrupted]``.
    """
    import server

    pipeline = server.MediaStreamSession(
        websocket=None, lang="en", media_transport=GenerationTrackingTransport(),
    )
    pipeline._smartpbx_transfer_context = object()
    pipeline._active_smartpbx_turn_id = "stalled-turn"
    spoken = []

    async def tts(text, *, sentence=None, turn_generation=None):
        spoken.append(text)
        pipeline._is_speaking = True
        await pipeline._send_tts_done(
            sentence=sentence, turn_generation=turn_generation,
        )

    monkeypatch.setattr(pipeline, "_tts_elevenlabs", tts)

    pipeline._start_assistant_turn_delivery_tracking()
    stalled_gen = pipeline._speak_generation

    fenced_gen = await pipeline._smartpbx_fence_stalled_generation(
        tts_tasks=[], gen=stalled_gen,
    )
    assert fenced_gen == pipeline._speak_generation == stalled_gen + 1
    assert pipeline._assistant_turn_generation == fenced_gen

    result = await pipeline._smartpbx_speak_recovery_and_finish(
        tool_executed=False, gen=fenced_gen, full_text="",
    )

    recovery = server.SMARTPBX_LLM_EMPTY_RETRY_RECOVERY_TEXT
    assert spoken == [recovery]
    assert result.endswith(recovery)
    assert pipeline._assistant_turn_generated_sentences == [recovery]
    assert pipeline._delivered_sentences == [recovery]
    assert pipeline._assistant_turn_was_interrupted() is False
    assert pipeline.history[-1] == {"role": "assistant", "content": recovery}
    pipeline._cancel_reprompt()


@pytest.mark.asyncio
async def test_filler_clear_resuming_after_barge_in_cannot_reanchor_old_turn(
    monkeypatch,
):
    """A clear already awaiting transport must revalidate ownership on return."""
    import server

    old_turn, new_turn = "filler-old-turn", "filler-new-turn"
    stale_answer = "This abandoned answer must never be delivered."
    transport = PauseFirstClearGenerationTransport()
    pipeline = server.MediaStreamSession(
        websocket=None, lang="en", media_transport=transport,
    )
    pipeline._smartpbx_transfer_context = object()
    pipeline._active_smartpbx_turn_id = old_turn
    filler_sleep = _ControlledInitialFillerSleep()
    filler_spoken = asyncio.Event()
    spoken = []
    real_controller = server.SmartPBXInitialFillerController

    def controlled_controller(**kwargs):
        return real_controller(**kwargs, sleep=filler_sleep)

    async def tts(text, *, sentence=None, turn_generation=None):
        spoken.append(text)
        pipeline._is_speaking = True
        await pipeline._send_tts_done(
            sentence=sentence, turn_generation=turn_generation,
        )
        if text == server.SMARTPBX_INITIAL_FILLER_TEXT:
            filler_spoken.set()

    monkeypatch.setattr(server, "SmartPBXInitialFillerController", controlled_controller)
    monkeypatch.setattr(pipeline, "_tts_elevenlabs", tts)

    pipeline._start_assistant_turn_delivery_tracking()
    generation = pipeline._speak_generation
    runner = server._SmartPBXRunnerContext(
        turn_id=old_turn,
        dropped_frame_baseline=0,
        speak_generation=generation,
        raw_utterance="",
    )
    token = server._smartpbx_runner_context.set(runner)
    try:
        controller = pipeline._start_initial_smartpbx_filler(
            round_idx=0, generation=generation,
        )
    finally:
        server._smartpbx_runner_context.reset(token)
    assert controller is not None

    stale_task = None
    try:
        await asyncio.wait_for(filler_sleep.entered.wait(), timeout=1)
        filler_sleep.release.set()
        await asyncio.wait_for(filler_spoken.wait(), timeout=1)

        async def finish_old_turn():
            await controller.on_content_delta()
            stale_generation = pipeline._runner_speak_generation(generation)
            await pipeline._invoke_speak(
                stale_answer,
                generation=stale_generation,
                sentence=stale_answer,
            )
            pipeline._append_assistant_history({
                "role": "assistant", "content": stale_answer,
            })

        token = server._smartpbx_runner_context.set(runner)
        try:
            stale_task = asyncio.create_task(finish_old_turn())
        finally:
            server._smartpbx_runner_context.reset(token)

        await asyncio.wait_for(transport.first_clear_entered.wait(), timeout=1)
        await pipeline._handle_bargein()
        pipeline._active_smartpbx_turn_id = new_turn
        history_after_barge = list(pipeline.history)
        transport.resume_first_clear.set()
        await asyncio.wait_for(stale_task, timeout=1)

        assert transport.clears == 2
        assert pipeline._smartpbx_barge_ins == 1
        assert pipeline._active_smartpbx_turn_id == new_turn
        assert runner.speak_generation == generation
        assert pipeline._assistant_turn_generation == generation
        assert pipeline._assistant_turn_was_interrupted() is True
        assert spoken == [server.SMARTPBX_INITIAL_FILLER_TEXT]
        assert pipeline.history == history_after_barge
    finally:
        filler_sleep.release.set()
        transport.resume_first_clear.set()
        if stale_task is not None:
            await asyncio.gather(stale_task, return_exceptions=True)
        await pipeline._finish_initial_smartpbx_filler(controller)
        pipeline._cancel_reprompt()


@pytest.mark.asyncio
async def test_timeout_clear_resuming_after_barge_in_cannot_reanchor_old_turn(
    monkeypatch,
):
    """A stalled runner must stay fenced when ownership changes during clear."""
    import server

    old_turn, new_turn = "timeout-old-turn", "timeout-new-turn"
    transport = PauseFirstClearGenerationTransport()
    pipeline = server.MediaStreamSession(
        websocket=None, lang="en", media_transport=transport,
    )
    pipeline._smartpbx_transfer_context = object()
    pipeline._active_smartpbx_turn_id = old_turn
    spoken = []

    async def tts(text, *, sentence=None, turn_generation=None):
        spoken.append(text)
        pipeline._is_speaking = True
        await pipeline._send_tts_done(
            sentence=sentence, turn_generation=turn_generation,
        )

    monkeypatch.setattr(pipeline, "_tts_elevenlabs", tts)

    pipeline._start_assistant_turn_delivery_tracking()
    generation = pipeline._speak_generation
    runner = server._SmartPBXRunnerContext(
        turn_id=old_turn,
        dropped_frame_baseline=0,
        speak_generation=generation,
        raw_utterance="",
    )

    async def finish_stalled_turn():
        return await pipeline._smartpbx_handle_stream_timeout(
            server._SmartPBXStreamTimeout(
                phase=server._SmartPBXStreamTimeout.PHASE_STALL,
            ),
            provider="claude",
            tool_executed=False,
            gen=generation,
            full_text="",
        )

    token = server._smartpbx_runner_context.set(runner)
    try:
        timeout_task = asyncio.create_task(finish_stalled_turn())
    finally:
        server._smartpbx_runner_context.reset(token)

    try:
        await asyncio.wait_for(transport.first_clear_entered.wait(), timeout=1)
        await pipeline._handle_bargein()
        pipeline._active_smartpbx_turn_id = new_turn
        history_after_barge = list(pipeline.history)
        transport.resume_first_clear.set()
        result = await asyncio.wait_for(timeout_task, timeout=1)

        assert transport.clears == 2
        assert pipeline._smartpbx_barge_ins == 1
        assert pipeline._active_smartpbx_turn_id == new_turn
        assert runner.speak_generation == generation
        assert pipeline._assistant_turn_generation == generation
        assert pipeline._assistant_turn_was_interrupted() is True
        assert spoken == []
        assert pipeline.history == history_after_barge
        assert result == ""
    finally:
        transport.resume_first_clear.set()
        await asyncio.gather(timeout_task, return_exceptions=True)
        pipeline._cancel_reprompt()
