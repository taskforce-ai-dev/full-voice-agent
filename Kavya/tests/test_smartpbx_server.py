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


async def _direct_tool_event_stream(events):
    for event in events:
        yield event


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
    return [SimpleNamespace(
        type="content_block_delta",
        delta=SimpleNamespace(type="text_delta", text=text),
    )]


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
    """Only direct Dialog sessions get the 120-token caller-rhythm budget."""
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
    else:
        assert request["max_tokens"] == 120


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
    assert resolved == 2.5


class _ControlledInitialFillerSleep:
    def __init__(self):
        self.delays = []
        self.release = asyncio.Event()

    async def __call__(self, seconds):
        self.delays.append(seconds)
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
async def test_spoken_initial_filler_is_cleared_before_a_side_effecting_tool_runs(tool_name):
    import server

    sleep = _ControlledInitialFillerSleep()
    filler_started = asyncio.Event()
    filler_cancelled = asyncio.Event()
    events = []

    async def speak(_text, *, generation):
        events.append(("filler", generation))
        filler_started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            filler_cancelled.set()
            raise

    async def clear_audio():
        events.append(("clear", None))

    controller = _initial_filler_controller(server, sleep, speak, clear_audio=clear_audio)
    controller.start()
    await asyncio.sleep(0)
    sleep.release.set()
    await asyncio.wait_for(filler_started.wait(), timeout=1)

    await controller.on_tool_delta()
    await asyncio.wait_for(filler_cancelled.wait(), timeout=1)
    events.append((tool_name, None))

    assert events == [("filler", 7), ("clear", None), (tool_name, None)]
    assert controller.suppress_specialized_tool_filler is True


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
