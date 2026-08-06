"""Kavya SmartPBX media-session and service-mode contract tests."""

import asyncio
import logging

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
    assert status["active_sessions"] == 0
    assert status["max_sessions"] == 4
    assert "token" not in status
    assert "account_id" not in status
    assert "do-not-expose" not in repr(status)


def test_unknown_service_mode_fails_closed():
    import server

    with pytest.raises(ValueError, match="invalid KAVYA_SERVICE_MODE"):
        server.build_service_app("both", {})
