"""Kavya SmartPBX media-session and service-mode contract tests."""

import asyncio
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
    assert request["url"] == "https://api.elevenlabs.io/v1/text-to-speech/unit-test-canonical-voice/stream?output_format=ulaw_8000"
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
    assert handover_context.get() == {}


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
    assert handover_context.get() == {}

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
    assert handover_context.get() == {}



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

    status = routes["/smartpbx/status"].endpoint()
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

    assert routes["/smartpbx/status"].endpoint()["transfer_enabled"] is True


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
async def test_smartpbx_runtime_invalid_provider_fails_before_client_pipeline_or_stt(monkeypatch):
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
    session = KavyaSmartPBXSession(
        context(), FakeTransport(), stt_factory=configured_stt_factory,
        post_call_processor=process_post_call, welcome_text="",
        llm_provider="invalid-provider", model="invalid-provider-model",
    )

    with pytest.raises(ValueError) as exc_info:
        await session.start()

    assert str(exc_info.value) == "invalid LLM provider: invalid-provider"
    assert client_factory_calls == []
    assert pipeline_constructions == []
    assert stt_calls == []
