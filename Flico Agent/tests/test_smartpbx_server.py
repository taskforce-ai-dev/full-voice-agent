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
