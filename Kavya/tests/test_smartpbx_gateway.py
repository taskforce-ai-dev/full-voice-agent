"""Contract tests for Kavya SmartPBX admission and lifecycle boundaries."""

import asyncio
import base64
import json
from dataclasses import replace

import pytest

from smartpbx_gateway import SmartPBXGateway, SmartPBXSessionRegistry, SmartPBXSettings


START = {
    "event": "start",
    "start": {
        "callId": "call-1", "otherLegCallId": "other-1",
        "callerIdNumber": "caller-opaque", "calleeIdNumber": "callee-opaque",
        "accountId": "account-1",
        "mediaFormat": {"encoding": "g711_ulaw", "sampleRate": 8000},
    },
}


class FakeWebSocket:
    def __init__(self, messages=(), token="test-token", header="X-Kavya-SmartPBX-Token"):
        self.headers = {} if token is None else {header: token}
        self.messages = list(messages)
        self.accepted = False
        self.close_calls = []
        self.sent = []

    async def accept(self):
        self.accepted = True

    async def receive_text(self):
        if self.messages:
            message = self.messages.pop(0)
            if isinstance(message, BaseException):
                raise message
            return message if isinstance(message, str) else json.dumps(message)
        await asyncio.Future()

    async def close(self, code=1000, reason=""):
        self.close_calls.append((code, reason))

    async def send_text(self, message):
        self.sent.append(message)


class FakeSession:
    def __init__(self, context, transport):
        self.context = context
        self.transport = transport
        self.starts = 0
        self.audio = []
        self.finishes = []
        self.terminal_future = asyncio.get_running_loop().create_future()

    async def start(self):
        self.starts += 1

    async def feed_audio(self, audio):
        self.audio.append(audio)

    async def finish(self, schedule_post_call=False):
        self.finishes.append(schedule_post_call)


class Factory:
    def __init__(self):
        self.sessions = []

    async def __call__(self, context, transport):
        session = FakeSession(context, transport)
        self.sessions.append(session)
        return session


def settings(**overrides):
    environment = {
        "ENABLE_SMARTPBX_WSS": "true", "SMARTPBX_WS_TOKEN": "test-token",
        "SMARTPBX_ACCOUNT_ID": "account-1",
    }
    environment.update(overrides)
    return SmartPBXSettings.from_env(environment)


async def run(messages, *, configuration=None, registry=None, token="test-token", header="X-Kavya-SmartPBX-Token"):
    configuration = configuration or settings()
    registry = registry or SmartPBXSessionRegistry(configuration.max_calls)
    gateway = SmartPBXGateway(configuration, registry)
    socket = FakeWebSocket(messages, token=token, header=header)
    factory = Factory()
    await gateway.handle(socket, factory)
    return gateway, registry, socket, factory


def test_settings_default_to_the_kavya_token_header_and_documented_bounds():
    configuration = settings()

    assert configuration.auth_header_name == "X-Kavya-SmartPBX-Token"
    assert (configuration.max_calls, configuration.start_timeout_seconds, configuration.idle_timeout_seconds) == (4, 10, 90)
    assert (configuration.max_message_chars, configuration.max_audio_bytes, configuration.max_outbound_frames) == (65536, 32768, 128)
    assert "test-token" not in repr(configuration)


@pytest.mark.asyncio
async def test_gateway_checks_token_before_accepting():
    _, registry, socket, factory = await run([], token="wrong-token")

    assert socket.accepted is False
    assert socket.close_calls == [(1008, "unauthorized")]
    assert factory.sessions == []
    assert registry.snapshot()["active_sessions"] == 0


@pytest.mark.asyncio
async def test_gateway_rejects_fifth_call_before_start_and_releases_slots_once():
    configuration = settings()
    registry = SmartPBXSessionRegistry(configuration.max_calls)
    leases = [await registry.try_acquire() for _ in range(4)]
    socket = FakeWebSocket([])
    await SmartPBXGateway(configuration, registry).handle(socket, Factory())

    assert socket.accepted is False
    assert socket.close_calls == [(1013, "capacity unavailable")]
    for lease in leases:
        await lease.release()
        await lease.release()
    assert registry.snapshot()["active_sessions"] == 0
    assert registry.snapshot()["released_total"] == 4


@pytest.mark.asyncio
async def test_gateway_requires_start_then_forwards_audio_and_finishes_once():
    audio = b"\x01\x02"
    _, registry, socket, factory = await run([
        {"event": "connected"}, START,
        {"event": "media", "media": {"payload": base64.b64encode(audio).decode("ascii")}},
        {"event": "stop"},
    ])

    assert socket.close_calls == [(1000, "call ended")]
    assert factory.sessions[0].starts == 1
    assert factory.sessions[0].audio == [audio]
    assert factory.sessions[0].finishes == [True]
    assert registry.snapshot()["active_sessions"] == 0


@pytest.mark.asyncio
async def test_gateway_rejects_media_before_start_and_account_mismatch():
    _, _, early_socket, early_factory = await run([{"event": "media", "media": {"payload": "YQ=="}}])
    assert early_socket.close_calls == [(1008, "start required")]
    assert early_factory.sessions == []

    wrong_account = json.loads(json.dumps(START))
    wrong_account["start"]["accountId"] = "other-account"
    _, _, account_socket, account_factory = await run([wrong_account])
    assert account_socket.close_calls == [(1008, "account mismatch")]
    assert account_factory.sessions == []


@pytest.mark.asyncio
async def test_gateway_enforces_start_and_idle_deadlines():
    _, _, start_socket, _ = await run([], configuration=replace(settings(), start_timeout_seconds=0.01))
    assert start_socket.close_calls == [(1008, "start timeout")]

    _, _, idle_socket, factory = await run([START], configuration=replace(settings(), idle_timeout_seconds=0.01))
    assert idle_socket.close_calls == [(1008, "idle timeout")]
    assert factory.sessions[0].finishes == [True]



@pytest.mark.asyncio
async def test_gateway_normally_closes_when_the_session_terminal_future_completes_with_none():
    factory = Factory()
    gateway = SmartPBXGateway(settings(), SmartPBXSessionRegistry(4))
    socket = FakeWebSocket([START])
    task = asyncio.create_task(gateway.handle(socket, factory))
    while not factory.sessions:
        await asyncio.sleep(0)
    factory.sessions[0].terminal_future.set_result(None)
    await task

    assert socket.close_calls == [(1000, "call ended")]
    assert factory.sessions[0].finishes == [True]
