"""Server integration tests for the direct SmartPBX media path."""

import asyncio
import base64
import json
import os
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace

import pytest

pytest.importorskip("httpx")
pytest.importorskip("fastapi")
pytest.importorskip("anthropic")

from fastapi.testclient import TestClient  # noqa: E402
from starlette.websockets import WebSocketDisconnect  # noqa: E402

import server  # noqa: E402
import post_call  # noqa: E402
from smartpbx_gateway import (  # noqa: E402
    SmartPBXGateway,
    SmartPBXSessionRegistry,
    SmartPBXSettings,
)


START = {
    "event": "start",
    "start": {
        "callId": "call-1",
        "otherLegCallId": "other-1",
        "callerIdNumber": "+15550000001",
        "calleeIdNumber": "+15550000000",
        "accountId": "account-1",
        "mediaFormat": {"encoding": "g711_ulaw", "sampleRate": 8000},
    },
}


class RouteSession:
    def __init__(self, context, transport):
        self.context = context
        self.transport = transport
        self.audio = []
        self.finished = 0

    async def start(self):
        await self.transport.send_audio(b"\x01\x02")
        await asyncio.sleep(0)

    async def feed_audio(self, audio):
        self.audio.append(audio)

    async def finish(self, schedule_post_call=False):
        self.finished += 1


class RouteFactory:
    def __init__(self):
        self.sessions = []

    async def __call__(self, context, transport):
        session = RouteSession(context, transport)
        self.sessions.append(session)
        return session


class FakeCallControl:
    def __init__(self, result=True, order=None):
        self.result = result
        self.order = order if order is not None else []
        self.destinations = []

    async def transfer_call(self, destination):
        self.order.append("transfer")
        self.destinations.append(destination)
        return self.result

    async def hangup_call(self):
        return True


class FakeClaudeStream:
    def __init__(self, content):
        self.content = content

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, traceback):
        return False

    def __aiter__(self):
        return self

    async def __anext__(self):
        raise StopAsyncIteration

    async def get_final_message(self):
        return SimpleNamespace(content=self.content)


class FakeClaudeMessages:
    def __init__(self, stream):
        self._stream = stream
        self.kwargs = None

    def stream(self, **kwargs):
        self.kwargs = kwargs
        return self._stream


def enabled_gateway():
    settings = SmartPBXSettings.from_env({
        "ENABLE_SMARTPBX_WSS": "true",
        "SMARTPBX_WS_TOKEN": "shared-secret",
        "SMARTPBX_ACCOUNT_ID": "account-1",
    })
    return SmartPBXGateway(settings, SmartPBXSessionRegistry(settings.max_calls))


def test_smartpbx_route_is_registered_exactly_once():
    paths = [route.path for route in server.app.routes]

    assert paths.count("/ws/v1/smartpbx/media") == 1


def test_smartpbx_route_rejects_valid_configuration_outside_service_mode(monkeypatch):
    class NoSessionFactory:
        async def __call__(self, context, transport):
            raise AssertionError("legacy mode must not construct a SmartPBX session")

    monkeypatch.setattr(server, "SMARTPBX_SERVICE_MODE", False)
    monkeypatch.setattr(server, "SMARTPBX_GATEWAY", enabled_gateway())
    monkeypatch.setattr(server, "SMARTPBX_SESSION_FACTORY", NoSessionFactory())
    client = TestClient(server.app, raise_server_exceptions=True)

    with pytest.raises(WebSocketDisconnect) as closed:
        with client.websocket_connect(
            "/ws/v1/smartpbx/media",
            headers={"X-Flico-SmartPBX-Token": "shared-secret"},
        ):
            pass

    assert closed.value.code == 1008


def test_existing_health_and_voice_routes_are_unchanged():
    client = TestClient(server.app, raise_server_exceptions=True)

    health = client.get("/health")
    voice = client.post("/voice/incoming")

    assert health.status_code == 200
    assert health.json()["status"] == "ok"
    assert voice.status_code == 200
    assert "<ConversationRelay" in voice.text


def test_smartpbx_status_is_operational_only(monkeypatch):
    gateway = enabled_gateway()
    monkeypatch.setattr(server, "SMARTPBX_GATEWAY", gateway, raising=False)
    client = TestClient(server.app, raise_server_exceptions=True)

    response = client.get("/smartpbx/status")

    assert response.status_code == 200
    payload = response.json()
    serialized = json.dumps(payload)
    assert set(payload) == {
        "enabled", "configured", "active_sessions", "max_sessions",
        "admitted_total", "rejected_capacity_total", "released_total",
        "protocol_version", "unknown_events_total",
    }
    for sensitive in (
        "shared-secret", "account-1", "+15550000001", "call-1", "other-1",
        "api_key", "caller", "call_ids",
    ):
        assert sensitive not in serialized


def test_canonical_websocket_exchange_emits_documented_media(monkeypatch):
    factory = RouteFactory()
    monkeypatch.setattr(server, "SMARTPBX_SERVICE_MODE", True)
    monkeypatch.setattr(server, "SMARTPBX_GATEWAY", enabled_gateway(), raising=False)
    monkeypatch.setattr(server, "SMARTPBX_SESSION_FACTORY", factory, raising=False)
    client = TestClient(server.app, raise_server_exceptions=True)

    with client.websocket_connect(
        "/ws/v1/smartpbx/media",
        headers={"X-Flico-SmartPBX-Token": "shared-secret"},
    ) as websocket:
        websocket.send_json(START)
        assert websocket.receive_json() == {
            "event": "media", "callId": "call-1", "accountId": "account-1",
            "media": {"payload": base64.b64encode(b"\x01\x02").decode("ascii")},
        }
        websocket.send_json({"event": "stop"})
        with pytest.raises(WebSocketDisconnect) as closed:
            websocket.receive_text()

    assert closed.value.code == 1000
    assert factory.sessions[0].finished == 1


def test_server_imports_disabled_without_mcp_credentials():
    environment = os.environ.copy()
    for name in list(environment):
        if name.startswith("SMARTPBX_") or name == "ENABLE_SMARTPBX_WSS":
            environment.pop(name)
    environment["ENABLE_ASTERISK_ARI"] = "false"

    result = subprocess.run(
        [sys.executable, "-c", "import server"],
        cwd=Path(server.__file__).parent,
        env=environment,
        text=True,
        capture_output=True,
        timeout=30,
    )

    assert result.returncode == 0, result.stderr


def test_smartpbx_modules_do_not_import_legacy_telephony_runtimes():
    root = Path(server.__file__).parent
    smartpbx_source = "\n".join(
        path.read_text(encoding="utf-8")
        for path in root.glob("smartpbx_*.py")
    ).lower()

    assert "asterisk_ari" not in smartpbx_source
    assert "asterisk_rtp" not in smartpbx_source
    assert "twilio" not in smartpbx_source
    assert server.ENABLE_ASTERISK_ARI is False


@pytest.mark.asyncio
async def test_english_smartpbx_claude_tool_uses_only_allowlisted_live_agent(monkeypatch):
    order = []
    call_control = FakeCallControl(order=order)
    block = SimpleNamespace(
        type="tool_use", name="transfer_to_human",
        input={"reason": "model controlled but not a destination"},
    )
    messages = FakeClaudeMessages(FakeClaudeStream([block]))
    client = SimpleNamespace(messages=messages)
    session = server.MediaStreamSession(
        websocket=None, lang="en", anthropic_client=client,
        transport=SimpleNamespace(), call_control=call_control,
    )

    async def speak(text, generation=-1):
        order.append(("speak", text))

    monkeypatch.setattr(session, "_speak", speak)
    response = await session._run_llm_claude()

    assert response == ""
    assert messages.kwargs["tools"] == [server.TRANSFER_TOOL]
    assert order[0][0] == "speak"
    assert order[1] == "transfer"
    assert call_control.destinations == ["live_agent"]
    assert session._active is False


def test_call_control_tool_is_smartpbx_english_only():
    control = FakeCallControl()

    english = server.MediaStreamSession(None, "en", transport=SimpleNamespace(), call_control=control)
    tamil = server.MediaStreamSession(None, "ta", transport=SimpleNamespace(), call_control=control)
    legacy = server.MediaStreamSession(None, "en", transport=SimpleNamespace())

    assert english.tools == [server.TRANSFER_TOOL]
    assert tamil.tools == []
    assert legacy.tools == []


@pytest.mark.asyncio
async def test_pipeline_failure_attempts_allowlisted_fallback_once(monkeypatch):
    control = FakeCallControl(result=False)
    session = server.MediaStreamSession(
        None, "en", anthropic_client=object(), transport=SimpleNamespace(),
        call_control=control,
    )

    async def fail_llm():
        raise RuntimeError("pipeline failed")

    async def no_op_speak(text, generation=-1):
        return None

    monkeypatch.setattr(server, "retrieve_context", lambda text, sticky: "")
    monkeypatch.setattr(session, "_run_llm_claude", fail_llm)
    monkeypatch.setattr(session, "_speak", no_op_speak)

    await session._process_utterance("please help")
    await session._process_utterance("please help again")

    assert control.destinations == ["live_agent"]


@pytest.mark.asyncio
async def test_finish_schedules_post_call_once_but_asterisk_stop_does_not(monkeypatch):
    scheduled = []

    async def post_call(**kwargs):
        scheduled.append(kwargs)

    class FakeStt:
        def __init__(self):
            self.stops = 0

        def stop(self):
            self.stops += 1

    monkeypatch.setattr(server, "process_realestate_post_call", post_call)
    smartpbx = server.MediaStreamSession(None, "en", transport=SimpleNamespace())
    smartpbx.history = [{"role": "user", "content": "hello"}]
    smartpbx._stt = FakeStt()

    await smartpbx.finish(schedule_post_call=True)
    await smartpbx.finish(schedule_post_call=True)
    await asyncio.sleep(0)

    assert smartpbx._stt is None
    assert len(scheduled) == 1

    asterisk = server.MediaStreamSession(None, "en", transport=SimpleNamespace())
    asterisk.history = [{"role": "user", "content": "hello"}]
    asterisk._stt = FakeStt()
    await asterisk.stop()
    await asyncio.sleep(0)
    assert len(scheduled) == 1


def test_google_stt_queue_is_bounded_by_frames_bytes_and_counts_drop_new(monkeypatch):
    monkeypatch.setattr(server, "GOOGLE_STT_AVAILABLE", False)
    stream = server.GoogleSTTStream(
        lambda text: None,
        max_queue_frames=3,
        max_queue_bytes=10,
        privacy_safe=True,
    )
    stream._running = True

    assert stream.feed(b"1234") is True
    assert stream.feed(b"5678") is True
    assert stream.feed(b"abc") is False
    assert stream.feed(b"zz") is True
    assert stream.feed(b"overflow") is False
    assert stream.queue_snapshot == {
        "queued_frames": 3,
        "queued_bytes": 10,
        "dropped_total": 2,
        "max_frames": 3,
        "max_bytes": 10,
    }


@pytest.mark.parametrize("frames,bytes_", [(0, 1), (1, 0), (-1, 1), (1, -1)])
def test_google_stt_queue_rejects_non_positive_bounds(frames, bytes_):
    with pytest.raises(ValueError, match="queue bounds"):
        server.GoogleSTTStream(
            lambda text: None,
            max_queue_frames=frames,
            max_queue_bytes=bytes_,
        )


def test_smartpbx_service_mode_does_not_import_legacy_runtimes():
    environment = os.environ.copy()
    environment["FLICO_SERVICE_MODE"] = "smartpbx"
    environment["ENABLE_SMARTPBX_WSS"] = "false"
    for name in (
        "SMARTPBX_WS_TOKEN", "SMARTPBX_ACCOUNT_ID", "SMARTPBX_API_KEY",
        "SMARTPBX_MCP_ACCOUNT_HEADER", "SMARTPBX_TRANSFER_DESTINATIONS_JSON",
    ):
        environment.pop(name, None)
    script = """
import json
import sys
import server
paths = [route.path for route in server.app.routes]
print(json.dumps({
    "route": paths.count("/ws/v1/smartpbx/media"),
    "legacy": [name for name in ("twilio", "twilio.rest", "asterisk_ari", "asterisk_rtp") if name in sys.modules],
    "asterisk_enabled": server.ENABLE_ASTERISK_ARI,
}))
"""

    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=Path(server.__file__).parent,
        env=environment,
        text=True,
        capture_output=True,
        timeout=60,
    )

    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout.strip().splitlines()[-1])
    assert payload == {"route": 1, "legacy": [], "asterisk_enabled": False}


@pytest.mark.asyncio
async def test_smartpbx_privacy_mode_redacts_session_content_and_exceptions(
    monkeypatch, caplog
):
    sentinel = "PRIVATE-call-account-phone-transcript-provider-body"
    control = FakeCallControl(result=False)
    session = server.MediaStreamSession(
        None, "en", anthropic_client=object(), transport=SimpleNamespace(),
        call_sid=sentinel, caller_phone=sentinel, call_control=control,
        privacy_safe=True, log_fingerprint="fingerprint-123",
    )
    caplog.set_level("DEBUG", logger="server")

    session._on_stt_result(sentinel)
    session._pending_transcript = sentinel
    session._event_loop = asyncio.get_running_loop()

    original_process = session._process_utterance
    async def fail_process(text):
        raise RuntimeError(sentinel)

    monkeypatch.setattr(session, "_process_utterance", fail_process)
    with pytest.raises(RuntimeError):
        await session._flush_transcript()

    monkeypatch.setattr(session, "_process_utterance", original_process)
    async def fail_llm():
        raise RuntimeError(sentinel)

    async def no_op_speak(text, generation=-1):
        return None

    monkeypatch.setattr(server, "retrieve_context", lambda text, sticky: "")
    monkeypatch.setattr(session, "_run_llm_claude", fail_llm)
    monkeypatch.setattr(session, "_speak", no_op_speak)
    await session._process_utterance(sentinel)

    output = caplog.text
    assert sentinel not in output
    assert "fingerprint-123" in output
    assert control.destinations == ["live_agent"]


def test_azure_stt_privacy_mode_redacts_transcripts_and_cancel_details(
    monkeypatch, caplog
):
    sentinel = "PRIVATE-azure-transcript-provider-detail"
    finals = []
    interims = []
    stream = server.AzureSTTStream(
        finals.append, interims.append, "en", privacy_safe=True,
    )
    recognized = object()
    monkeypatch.setattr(
        server, "azure_speech",
        SimpleNamespace(ResultReason=SimpleNamespace(RecognizedSpeech=recognized)),
    )
    caplog.set_level("INFO", logger="server")
    interim_event = SimpleNamespace(result=SimpleNamespace(text=sentinel))
    final_event = SimpleNamespace(
        result=SimpleNamespace(text=sentinel, reason=recognized)
    )
    canceled_event = SimpleNamespace(reason=sentinel, error_details=sentinel)

    stream._on_recognizing(interim_event)
    stream._on_recognized(final_event)
    stream._on_canceled(canceled_event)

    assert interims == [sentinel]
    assert finals == [sentinel]
    assert sentinel not in caplog.text


def test_privacy_safe_stt_factory_allows_azure_when_selected(monkeypatch):
    monkeypatch.setattr(server, "STT_PROVIDER", "azure")
    monkeypatch.setattr(server, "AZURE_STT_AVAILABLE", True)
    monkeypatch.setattr(server, "audioop", object())

    stream = server._make_stt(
        lambda text: None, lambda text: None, "en", privacy_safe=True,
    )

    assert isinstance(stream, server.AzureSTTStream)
    assert stream._privacy_safe is True


@pytest.mark.asyncio
async def test_privacy_safe_start_never_logs_real_call_or_phone(monkeypatch, caplog):
    sentinel = "PRIVATE-start-call-phone"
    session = server.MediaStreamSession(
        None, "en", transport=SimpleNamespace(), call_sid=sentinel,
        caller_phone=sentinel, privacy_safe=True, log_fingerprint="safe-fp",
    )
    monkeypatch.setattr(session, "_start_stt", lambda: True)
    monkeypatch.setattr(session, "_speak", lambda text: asyncio.sleep(3600))
    caplog.set_level("INFO", logger="server")

    await session.start()
    await session.finish()

    assert sentinel not in caplog.text
    assert "safe-fp" in caplog.text
    assert session._tasks == set()


def test_valid_token_fifth_call_receives_asgi_close_1013(monkeypatch):
    settings = SmartPBXSettings.from_env({
        "ENABLE_SMARTPBX_WSS": "true",
        "SMARTPBX_WS_TOKEN": "shared-secret",
        "SMARTPBX_ACCOUNT_ID": "account-1",
        "SMARTPBX_MAX_CALLS": "1",
    })
    registry = SmartPBXSessionRegistry(1)
    lease = asyncio.run(registry.try_acquire())
    assert lease is not None
    gateway = SmartPBXGateway(settings, registry)
    monkeypatch.setattr(server, "SMARTPBX_SERVICE_MODE", True)
    monkeypatch.setattr(server, "SMARTPBX_GATEWAY", gateway)
    client = TestClient(server.app, raise_server_exceptions=True)

    with client.websocket_connect(
        "/ws/v1/smartpbx/media",
        headers={"X-Flico-SmartPBX-Token": "shared-secret"},
    ) as websocket:
        with pytest.raises(WebSocketDisconnect) as closed:
            websocket.receive_text()

    assert closed.value.code == 1013
    assert registry.snapshot()["rejected_capacity_total"] == 1
    asyncio.run(lease.release())


@pytest.mark.asyncio
async def test_smartpbx_stt_unavailable_transfers_once_then_terminates(monkeypatch):
    control = FakeCallControl(result=False)
    session = server.MediaStreamSession(
        None, "en", transport=SimpleNamespace(), call_control=control,
        privacy_safe=True, log_fingerprint="safe-fp",
    )
    monkeypatch.setattr(session, "_start_stt", lambda: False)
    monkeypatch.setattr(session, "_speak", lambda text: asyncio.sleep(0))

    await session.start()

    assert session.terminal_future.result() == {
        "transferred": False, "failure_class": "stt_unavailable",
    }
    assert control.destinations == ["live_agent"]
    assert session._active is False


@pytest.mark.asyncio
async def test_smartpbx_stt_queue_overflow_transfers_once_and_stops_feeding(monkeypatch):
    control = FakeCallControl(result=False)
    session = server.MediaStreamSession(
        None, "en", transport=SimpleNamespace(), call_control=control,
        privacy_safe=True, log_fingerprint="safe-fp",
    )
    session._stt = SimpleNamespace(feed=lambda audio: False)
    monkeypatch.setattr(session, "_speak", lambda text: asyncio.sleep(0))

    await session.feed_audio(b"frame")
    await session.feed_audio(b"second-frame")

    assert session.terminal_future.result() == {
        "transferred": False, "failure_class": "stt_queue_overflow",
    }
    assert control.destinations == ["live_agent"]
    assert session._active is False


class FakeTtsResponse:
    def __init__(self, status_code=200, body=b""):
        self.status_code = status_code
        self._body = body

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, traceback):
        return False

    async def aread(self):
        return self._body

    async def aiter_bytes(self, chunk_size=640):
        if False:
            yield b""


class FakeTtsHttp:
    def __init__(self, response=None, error=None):
        self.response = response
        self.error = error

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, traceback):
        return False

    def stream(self, *args, **kwargs):
        if self.error is not None:
            raise self.error
        return self.response


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "failure,expected",
    [
        ("unavailable", "tts_unavailable"),
        ("status", "tts_status"),
        ("timeout", "tts_timeout"),
        ("exception", "tts_exception"),
    ],
)
async def test_smartpbx_elevenlabs_failures_transfer_once_without_logging_body(
    monkeypatch, caplog, failure, expected
):
    sentinel = "PRIVATE-tts-text-provider-body"
    control = FakeCallControl(result=False)
    session = server.MediaStreamSession(
        None, "en", transport=SimpleNamespace(), call_control=control,
        privacy_safe=True, log_fingerprint="safe-fp",
    )
    monkeypatch.setattr(session, "_speak", lambda text: asyncio.sleep(0))
    if failure == "unavailable":
        monkeypatch.setattr(server, "ELEVENLABS_API_KEY", "")
        monkeypatch.setattr(server, "ELEVENLABS_VOICE_ID", "")
    else:
        monkeypatch.setattr(server, "ELEVENLABS_API_KEY", "fake-key")
        monkeypatch.setattr(server, "ELEVENLABS_VOICE_ID", "fake-voice")
        if failure == "status":
            http = FakeTtsHttp(FakeTtsResponse(503, sentinel.encode()))
        elif failure == "timeout":
            http = FakeTtsHttp(error=server.httpx.TimeoutException(sentinel))
        else:
            http = FakeTtsHttp(error=RuntimeError(sentinel))
        monkeypatch.setattr(server.httpx, "AsyncClient", lambda: http)
    caplog.set_level("INFO", logger="server")

    await session._run_pipeline_task(session._tts_elevenlabs(sentinel))

    assert control.destinations == ["live_agent"]
    assert session._active is False
    assert expected in caplog.text
    assert sentinel not in caplog.text


@pytest.mark.asyncio
async def test_detached_smartpbx_post_call_keeps_pii_only_in_payload(monkeypatch, caplog):
    sentinel = "PRIVATE-detached-call-phone-transcript-provider-body"
    fingerprint = "safe-postcall-fp"
    captured = []
    completed = asyncio.Event()

    class Response:
        status_code = 503
        text = sentinel

    class Http:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, traceback):
            return False

        async def post(self, url, json):
            captured.append(json)
            return Response()

    real_process = post_call.process_realestate_post_call

    async def observed_process(**kwargs):
        try:
            await real_process(**kwargs)
        finally:
            completed.set()

    monkeypatch.setattr(post_call.httpx, "AsyncClient", Http)
    monkeypatch.setattr(server, "process_realestate_post_call", observed_process)
    session = server.MediaStreamSession(
        None, "en", transport=SimpleNamespace(), call_sid=sentinel,
        caller_phone=sentinel, privacy_safe=True, log_fingerprint=fingerprint,
    )
    session.history = [{"role": "user", "content": sentinel}]
    caplog.set_level("INFO")

    session._schedule_post_call()
    await asyncio.wait_for(completed.wait(), timeout=2)

    assert captured[0]["call_id"] == sentinel
    assert sentinel in captured[0]["transcript"]
    assert sentinel not in caplog.text
    assert fingerprint in caplog.text


@pytest.mark.asyncio
async def test_tts_sibling_failure_cancels_and_drains_all_tracked_tasks():
    session = server.MediaStreamSession(
        None, "en", transport=SimpleNamespace(),
        privacy_safe=True, log_fingerprint="safe-fp",
    )
    sibling_cancelled = asyncio.Event()

    async def fail():
        await asyncio.sleep(0)
        raise server.PipelineFailure("tts_status")

    async def wait_forever():
        try:
            await asyncio.Future()
        finally:
            sibling_cancelled.set()

    tasks = [session._spawn(fail()), session._spawn(wait_forever())]
    with pytest.raises(server.PipelineFailure):
        await session._await_tts_tasks(tasks)
    await asyncio.sleep(0)

    assert sibling_cancelled.is_set()
    assert session._tasks == set()


def test_google_stt_runtime_failure_exhaustion_signals_once_without_detail(caplog):
    sentinel = "PRIVATE-google-provider-runtime-detail"
    failures = []
    stream = server.GoogleSTTStream(
        lambda text: None, privacy_safe=True, max_runtime_failures=3,
        retry_backoff_seconds=0, on_runtime_failure=failures.append,
    )
    attempts = 0

    def fail_stream():
        nonlocal attempts
        attempts += 1
        raise RuntimeError(sentinel)

    stream._running = True
    stream._run_one_stream = fail_stream
    caplog.set_level("INFO", logger="server")
    stream._loop()

    assert attempts == 3
    assert failures == ["stt_unavailable"]
    assert stream._running is False
    assert sentinel not in caplog.text


def test_smartpbx_service_mode_blocks_legacy_http_and_websockets(monkeypatch):
    monkeypatch.setattr(server, "SMARTPBX_SERVICE_MODE", True)
    client = TestClient(server.app, raise_server_exceptions=True)

    assert client.get("/voice/incoming").status_code == 404
    assert client.get("/openapi.json").status_code == 404
    assert client.get("/health").status_code == 200
    assert client.get("/smartpbx/status").status_code == 200
    with pytest.raises(WebSocketDisconnect) as conversation_closed:
        with client.websocket_connect("/ws/conversation"):
            pass
    with pytest.raises(WebSocketDisconnect) as media_closed:
        with client.websocket_connect("/ws/media-stream/ta"):
            pass

    assert conversation_closed.value.code == 1008
    assert media_closed.value.code == 1008


@pytest.mark.asyncio
async def test_real_media_session_fatal_future_closes_gateway_and_releases(monkeypatch):
    settings = SmartPBXSettings.from_env({
        "ENABLE_SMARTPBX_WSS": "true",
        "SMARTPBX_WS_TOKEN": "shared-secret",
        "SMARTPBX_ACCOUNT_ID": "account-1",
        "SMARTPBX_MAX_CALLS": "1",
    })
    registry = SmartPBXSessionRegistry(1)
    gateway = SmartPBXGateway(settings, registry)
    sessions = []

    async def factory(context, transport):
        session = server.MediaStreamSession(
            None, "en", transport=transport, call_sid=context.call_id,
            caller_phone=context.caller_id_number, privacy_safe=True,
            log_fingerprint="safe-real-fp",
        )
        sessions.append(session)
        return session

    monkeypatch.setattr(server.MediaStreamSession, "_start_stt", lambda self: True)
    monkeypatch.setattr(server, "ELEVENLABS_API_KEY", "")
    monkeypatch.setattr(server, "ELEVENLABS_VOICE_ID", "")
    monkeypatch.setattr(server, "SMARTPBX_SERVICE_MODE", True)
    monkeypatch.setattr(server, "SMARTPBX_GATEWAY", gateway)
    monkeypatch.setattr(server, "SMARTPBX_SESSION_FACTORY", factory)
    client = TestClient(server.app, raise_server_exceptions=True)

    with client.websocket_connect(
        "/ws/v1/smartpbx/media",
        headers={"X-Flico-SmartPBX-Token": "shared-secret"},
    ) as websocket:
        websocket.send_json(START)
        with pytest.raises(WebSocketDisconnect) as closed:
            websocket.receive_text()

    assert closed.value.code == 1011
    assert sessions[0].terminal_future.result() == {
        "transferred": False, "failure_class": "tts_unavailable",
    }
    assert registry.snapshot()["active_sessions"] == 0
    assert registry.snapshot()["released_total"] == 1


@pytest.mark.asyncio
async def test_failed_claude_transfer_is_terminal_and_attempted_once(monkeypatch):
    control = FakeCallControl(result=False)
    block = SimpleNamespace(
        type="tool_use", name="transfer_to_human", input={"reason": "help"},
    )
    session = server.MediaStreamSession(
        None, "en",
        anthropic_client=SimpleNamespace(
            messages=FakeClaudeMessages(FakeClaudeStream([block]))
        ),
        transport=SimpleNamespace(), call_control=control,
        privacy_safe=True, log_fingerprint="safe-fp",
    )

    async def no_op_speak(text, generation=-1):
        return None

    monkeypatch.setattr(session, "_speak", no_op_speak)

    assert await session._run_llm_claude() == ""
    assert await session._run_llm_claude() == ""
    assert session.terminal_future.result() == {
        "transferred": False, "failure_class": "transfer",
    }
    assert control.destinations == ["live_agent"]
    assert session._active is False


@pytest.mark.asyncio
async def test_finished_smartpbx_session_ignores_late_stt_callbacks(monkeypatch):
    control = FakeCallControl(result=False)
    session = server.MediaStreamSession(
        None, "en", transport=SimpleNamespace(), call_control=control,
        privacy_safe=True, log_fingerprint="safe-fp",
    )
    session._event_loop = asyncio.get_running_loop()
    processed = []

    async def record_process(text):
        processed.append(text)

    async def no_op_speak(text, generation=-1):
        return None

    monkeypatch.setattr(session, "_process_utterance", record_process)
    monkeypatch.setattr(session, "_speak", no_op_speak)

    await session.finish()
    session._on_stt_result("late transcript")
    session._on_stt_interim("late interim")
    session._on_stt_runtime_failure("stt_runtime")
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    await session._flush_transcript()

    assert processed == []
    assert control.destinations == []
    assert session._fatal_future is None
    assert session._tasks == set()


@pytest.mark.asyncio
async def test_successful_claude_transfer_resolves_terminal_once(monkeypatch):
    control = FakeCallControl(result=True)
    block = SimpleNamespace(
        type="tool_use", name="transfer_to_human", input={"reason": "help"},
    )
    session = server.MediaStreamSession(
        None, "en",
        anthropic_client=SimpleNamespace(
            messages=FakeClaudeMessages(FakeClaudeStream([block]))
        ),
        transport=SimpleNamespace(), call_control=control,
        privacy_safe=True, log_fingerprint="safe-fp",
    )

    async def no_op_speak(text, generation=-1):
        return None

    monkeypatch.setattr(session, "_speak", no_op_speak)

    assert await session._run_llm_claude() == ""
    assert await session._run_llm_claude() == ""
    assert session.terminal_future.result() == {
        "transferred": True, "failure_class": "transfer",
    }
    assert control.destinations == ["live_agent"]
    assert session._active is False
@pytest.mark.asyncio
async def test_default_disabled_mcp_configuration_builds_session_without_call_control(
    monkeypatch,
):
    monkeypatch.setattr(server, "LLM_PROVIDER", "claude")
    monkeypatch.setattr(server, "_get_anthropic_client", lambda: object())
    monkeypatch.setenv(
        "SMARTPBX_MCP_URL", "https://dialog.cybergate.lk:9443/ucp/v2/mcp"
    )
    monkeypatch.setenv("SMARTPBX_ACCOUNT_ID", "account-1")
    monkeypatch.setenv("SMARTPBX_TRANSFER_DESTINATIONS_JSON", "{}")
    monkeypatch.delenv("SMARTPBX_API_KEY", raising=False)
    monkeypatch.delenv("SMARTPBX_MCP_ACCOUNT_HEADER", raising=False)

    session = await server._make_smartpbx_session(
        SimpleNamespace(
            call_id="call-1",
            caller_id_number="+15550000001",
            account_id="account-1",
        ),
        SimpleNamespace(),
    )

    assert session.call_control is None


def test_legacy_server_import_ignores_disabled_malformed_smartpbx_settings():
    environment = os.environ.copy()
    environment.update({
        "FLICO_SERVICE_MODE": "legacy",
        "ENABLE_ASTERISK_ARI": "false",
        "ENABLE_SMARTPBX_WSS": "false",
        "SMARTPBX_WS_TOKEN": "stale-token",
        "SMARTPBX_ACCOUNT_ID": "stale-account",
        "SMARTPBX_MAX_CALLS": "invalid",
        "SMARTPBX_MAX_MESSAGE_CHARS": "999999",
        "SMARTPBX_MAX_AUDIO_BYTES": "invalid",
        "SMARTPBX_MAX_OUTBOUND_FRAMES": "0",
        "SMARTPBX_START_TIMEOUT_SECONDS": "-1",
        "SMARTPBX_IDLE_TIMEOUT_SECONDS": "invalid",
    })

    result = subprocess.run(
        [sys.executable, "-c", "import server"],
        cwd=Path(server.__file__).parent,
        env=environment,
        text=True,
        capture_output=True,
        timeout=60,
    )

    assert result.returncode == 0, result.stderr
