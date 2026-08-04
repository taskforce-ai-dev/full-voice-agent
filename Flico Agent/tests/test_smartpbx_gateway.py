"""Contract tests for bounded SmartPBX configuration, admission, and lifecycle."""

import asyncio
import base64
import json
from dataclasses import replace

import pytest
from starlette.websockets import WebSocketDisconnect

import smartpbx_gateway as gateway_module
from smartpbx_gateway import (
    SMARTPBX_PROTOCOL_VERSION,
    SmartPBXSessionRegistry,
    SmartPBXSettings,
    smartpbx_status,
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


class FakeWebSocket:
    def __init__(self, messages=(), token="shared-secret"):
        self.headers = {} if token is None else {"X-Flico-SmartPBX-Token": token}
        self._messages = list(messages)
        self.accepted = False
        self.sent_text = []
        self.close_calls = []

    async def accept(self):
        self.accepted = True

    async def receive_text(self):
        if self._messages:
            value = self._messages.pop(0)
            if isinstance(value, BaseException):
                raise value
            return value if isinstance(value, str) else json.dumps(value)
        await asyncio.Future()

    async def send_text(self, value):
        self.sent_text.append(value)

    async def close(self, code=1000, reason=""):
        self.close_calls.append((code, reason))


class FakeSession:
    def __init__(self, context, transport, *, audio_on_start=b""):
        self.context = context
        self.transport = transport
        self.audio_on_start = audio_on_start
        self.started = 0
        self.audio = []
        self.finish_calls = []

    async def start(self):
        self.started += 1
        if self.audio_on_start:
            await self.transport.send_audio(self.audio_on_start)
            await asyncio.sleep(0)

    async def feed_audio(self, audio):
        self.audio.append(audio)

    async def finish(self, schedule_post_call=False):
        self.finish_calls.append(schedule_post_call)


class SessionFactory:
    def __init__(self, *, audio_on_start=b""):
        self.audio_on_start = audio_on_start
        self.sessions = []

    async def __call__(self, context, transport):
        session = FakeSession(context, transport, audio_on_start=self.audio_on_start)
        self.sessions.append(session)
        return session


def make_gateway(settings=None, registry=None):
    gateway_type = getattr(gateway_module, "SmartPBXGateway")
    settings = settings or SmartPBXSettings.from_env(enabled_environment())
    registry = registry or SmartPBXSessionRegistry(settings.max_calls)
    return gateway_type(settings, registry), registry


async def run_gateway(messages, *, token="shared-secret", settings=None, registry=None,
                      factory=None):
    gateway, registry = make_gateway(settings, registry)
    websocket = FakeWebSocket(messages, token=token)
    factory = factory or SessionFactory()
    await gateway.handle(websocket, factory)
    return gateway, registry, websocket, factory


def enabled_environment(**overrides):
    environment = {
        "ENABLE_SMARTPBX_WSS": "true",
        "SMARTPBX_WS_TOKEN": "shared-secret",
        "SMARTPBX_ACCOUNT_ID": "account-1",
    }
    environment.update(overrides)
    return environment


def test_smartpbx_is_disabled_and_unconfigured_by_default():
    settings = SmartPBXSettings.from_env({})

    assert settings.enabled is False
    assert settings.configured is False
    assert settings.token_matches("shared-secret") is False
    assert "shared-secret" not in repr(settings)


def test_disabled_smartpbx_can_still_report_valid_configuration():
    settings = SmartPBXSettings.from_env({
        "SMARTPBX_WS_TOKEN": "shared-secret",
        "SMARTPBX_ACCOUNT_ID": "account-1",
    })

    assert settings.enabled is False
    assert settings.configured is True


@pytest.mark.parametrize(
    "environment",
    [
        {"ENABLE_SMARTPBX_WSS": "true", "SMARTPBX_ACCOUNT_ID": "account-1"},
        {"ENABLE_SMARTPBX_WSS": "true", "SMARTPBX_WS_TOKEN": "shared-secret"},
    ],
)
def test_enabled_smartpbx_requires_token_and_account(environment):
    with pytest.raises(ValueError, match="configuration"):
        SmartPBXSettings.from_env(environment)


def test_settings_enforces_documented_integer_bounds_and_authenticates_tokens():
    settings = SmartPBXSettings.from_env(enabled_environment(
        SMARTPBX_MAX_CALLS="4",
        SMARTPBX_MAX_MESSAGE_CHARS="65536",
        SMARTPBX_MAX_AUDIO_BYTES="32768",
        SMARTPBX_MAX_OUTBOUND_FRAMES="128",
        SMARTPBX_START_TIMEOUT_SECONDS="30",
        SMARTPBX_IDLE_TIMEOUT_SECONDS="300",
    ))

    assert settings.max_calls == 4
    assert settings.max_message_chars == 65536
    assert settings.max_audio_bytes == 32768
    assert settings.max_outbound_frames == 128
    assert settings.start_timeout_seconds == 30
    assert settings.idle_timeout_seconds == 300
    assert settings.token_matches("shared-secret") is True
    assert settings.token_matches("wrong-token") is False


@pytest.mark.parametrize(
    "name,value",
    [
        ("SMARTPBX_MAX_CALLS", "0"),
        ("SMARTPBX_MAX_CALLS", "5"),
        ("SMARTPBX_MAX_MESSAGE_CHARS", "1023"),
        ("SMARTPBX_MAX_MESSAGE_CHARS", "65537"),
        ("SMARTPBX_MAX_AUDIO_BYTES", "159"),
        ("SMARTPBX_MAX_AUDIO_BYTES", "32769"),
        ("SMARTPBX_MAX_OUTBOUND_FRAMES", "0"),
        ("SMARTPBX_MAX_OUTBOUND_FRAMES", "129"),
        ("SMARTPBX_START_TIMEOUT_SECONDS", "0"),
        ("SMARTPBX_START_TIMEOUT_SECONDS", "31"),
        ("SMARTPBX_IDLE_TIMEOUT_SECONDS", "9"),
        ("SMARTPBX_IDLE_TIMEOUT_SECONDS", "301"),
    ],
)
def test_settings_rejects_out_of_range_integers(name, value):
    with pytest.raises(ValueError, match="configuration"):
        SmartPBXSettings.from_env(enabled_environment(**{name: value}))


@pytest.mark.parametrize("value", ["", "one", "1.0", "-1"])
def test_settings_rejects_malformed_integers(value):
    with pytest.raises(ValueError, match="configuration"):
        SmartPBXSettings.from_env(enabled_environment(SMARTPBX_MAX_CALLS=value))


@pytest.mark.asyncio
async def test_registry_enforces_four_active_session_leases():
    registry = SmartPBXSessionRegistry(max_sessions=4)

    leases = [await registry.try_acquire() for _ in range(4)]
    rejected = await registry.try_acquire()

    assert all(lease is not None for lease in leases)
    assert rejected is None
    assert registry.snapshot() == {
        "active_sessions": 4,
        "max_sessions": 4,
        "admitted_total": 4,
        "rejected_capacity_total": 1,
        "released_total": 0,
    }


@pytest.mark.asyncio
async def test_releasing_a_lease_admits_another_without_underflow_on_double_release():
    registry = SmartPBXSessionRegistry(max_sessions=1)
    lease = await registry.try_acquire()
    assert lease is not None

    await lease.release()
    replacement = await registry.try_acquire()
    await lease.release()

    assert replacement is not None
    assert registry.snapshot() == {
        "active_sessions": 1,
        "max_sessions": 1,
        "admitted_total": 2,
        "rejected_capacity_total": 0,
        "released_total": 1,
    }


@pytest.mark.asyncio
async def test_cancelled_release_waiting_for_registry_lock_can_be_retried():
    registry = SmartPBXSessionRegistry(max_sessions=1)
    lease = await registry.try_acquire()
    assert lease is not None
    await registry._lock.acquire()
    release_task = asyncio.create_task(lease.release())
    await asyncio.sleep(0)

    release_task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await release_task
    registry._lock.release()

    await lease.release()
    replacement = await registry.try_acquire()

    assert replacement is not None
    await replacement.release()
    assert registry.snapshot() == {
        "active_sessions": 0,
        "max_sessions": 1,
        "admitted_total": 2,
        "rejected_capacity_total": 0,
        "released_total": 2,
    }


@pytest.mark.asyncio
async def test_registry_counters_saturate_at_signed_64_bit_maximum():
    registry = SmartPBXSessionRegistry(max_sessions=1)
    lease = await registry.try_acquire()
    assert lease is not None
    maximum = (1 << 63) - 1
    registry._admitted_total = maximum
    registry._rejected_capacity_total = maximum
    registry._released_total = maximum

    assert await registry.try_acquire() is None
    await lease.release()

    assert registry.snapshot() == {
        "active_sessions": 0,
        "max_sessions": 1,
        "admitted_total": maximum,
        "rejected_capacity_total": maximum,
        "released_total": maximum,
    }


@pytest.mark.asyncio
async def test_status_exposes_only_safe_bounded_operational_values():
    settings = SmartPBXSettings.from_env(enabled_environment())
    registry = SmartPBXSessionRegistry(max_sessions=settings.max_calls)
    for _ in range(4):
        assert await registry.try_acquire() is not None
    assert await registry.try_acquire() is None

    status = smartpbx_status(settings, registry)

    assert status == {
        "enabled": True,
        "configured": True,
        "active_sessions": 4,
        "max_sessions": 4,
        "admitted_total": 4,
        "rejected_capacity_total": 1,
        "released_total": 0,
        "protocol_version": SMARTPBX_PROTOCOL_VERSION,
    }
    assert set(status) == {
        "enabled",
        "configured",
        "active_sessions",
        "max_sessions",
        "admitted_total",
        "rejected_capacity_total",
        "released_total",
        "protocol_version",
    }
    assert "shared-secret" not in repr(status)


@pytest.mark.asyncio
@pytest.mark.parametrize("token", [None, "wrong-token"])
async def test_gateway_rejects_missing_or_wrong_token_before_accept(token):
    _, registry, websocket, factory = await run_gateway([], token=token)

    assert websocket.accepted is False
    assert websocket.close_calls == [(1008, "unauthorized")]
    assert factory.sessions == []
    assert registry.snapshot()["active_sessions"] == 0


@pytest.mark.asyncio
async def test_gateway_processes_canonical_call_and_cleans_up_once():
    audio = b"\x01\x02\x03"
    media = {
        "event": "media",
        "media": {"payload": base64.b64encode(audio).decode("ascii")},
    }
    gateway, registry, websocket, factory = await run_gateway([
        {"event": "connected"},
        START,
        media,
        {"event": "dtmf", "dtmf": {"digit": "5", "duration": 100}},
        {"event": "future-event", "safe": True},
        {"event": "stop"},
    ])

    assert websocket.accepted is True
    assert websocket.close_calls == [(1000, "call ended")]
    assert len(factory.sessions) == 1
    session = factory.sessions[0]
    assert session.started == 1
    assert session.audio == [audio]
    assert session.finish_calls == [True]
    assert session.transport.is_active is False
    assert registry.snapshot()["active_sessions"] == 0
    assert gateway.snapshot()["unknown_events_total"] == 1


@pytest.mark.asyncio
async def test_gateway_rejects_media_before_start():
    media = {
        "event": "media",
        "media": {"payload": base64.b64encode(b"audio").decode("ascii")},
    }
    _, registry, websocket, factory = await run_gateway([media])

    assert websocket.accepted is True
    assert websocket.close_calls == [(1008, "start required")]
    assert factory.sessions == []
    assert registry.snapshot()["active_sessions"] == 0


@pytest.mark.asyncio
async def test_gateway_capacity_rejection_happens_before_accept_and_is_reusable():
    settings = SmartPBXSettings.from_env(enabled_environment(SMARTPBX_MAX_CALLS="1"))
    registry = SmartPBXSessionRegistry(1)
    lease = await registry.try_acquire()
    assert lease is not None

    _, _, websocket, factory = await run_gateway(
        [], settings=settings, registry=registry,
    )
    assert websocket.accepted is False
    assert websocket.close_calls == [(1013, "capacity unavailable")]
    assert factory.sessions == []

    await lease.release()
    _, _, reusable_socket, reusable_factory = await run_gateway(
        [START, {"event": "stop"}], settings=settings, registry=registry,
    )
    assert reusable_socket.accepted is True
    assert len(reusable_factory.sessions) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "settings",
    [
        SmartPBXSettings.from_env({}),
        SmartPBXSettings(
            enabled=True, token="", account_id="", max_calls=4,
            max_message_chars=65536, max_audio_bytes=32768,
            max_outbound_frames=128, start_timeout_seconds=10,
            idle_timeout_seconds=90,
        ),
    ],
)
async def test_gateway_rejects_disabled_or_unconfigured_before_accept(settings):
    _, registry, websocket, factory = await run_gateway([], settings=settings)

    assert websocket.accepted is False
    assert websocket.close_calls == [(1008, "service unavailable")]
    assert factory.sessions == []
    assert registry.snapshot()["active_sessions"] == 0


@pytest.mark.asyncio
async def test_gateway_start_deadline_is_total_across_compatibility_events():
    settings = replace(
        SmartPBXSettings.from_env(enabled_environment()),
        start_timeout_seconds=0.01,
    )
    _, registry, websocket, factory = await run_gateway(
        [{"event": "connected"}, {"event": "future-event"}],
        settings=settings,
    )

    assert websocket.close_calls == [(1008, "start timeout")]
    assert factory.sessions == []
    assert registry.snapshot()["active_sessions"] == 0


@pytest.mark.asyncio
async def test_gateway_enforces_account_identity():
    wrong_account = json.loads(json.dumps(START))
    wrong_account["start"]["accountId"] = "other-account"
    _, _, websocket, factory = await run_gateway([wrong_account])

    assert websocket.close_calls == [(1008, "account mismatch")]
    assert factory.sessions == []


@pytest.mark.asyncio
async def test_gateway_rejects_duplicate_start_even_when_context_matches():
    _, _, websocket, factory = await run_gateway([START, START])

    assert websocket.close_calls == [(1008, "duplicate start")]
    assert factory.sessions[0].finish_calls == [True]


@pytest.mark.asyncio
async def test_gateway_rejects_invalid_base64_after_start():
    _, _, websocket, factory = await run_gateway([
        START,
        {"event": "media", "media": {"payload": "not base64!"}},
    ])

    assert websocket.close_calls == [(1008, "invalid media payload")]
    assert factory.sessions[0].finish_calls == [True]


@pytest.mark.asyncio
async def test_gateway_validates_hangup_context_and_accepts_matching_hangup():
    mismatch = {
        "event": "hangup",
        "hangup": {
            "callId": "different", "otherLegCallId": "other-1",
            "accountId": "account-1", "reason": "normal",
        },
    }
    _, _, mismatched_socket, mismatched_factory = await run_gateway([START, mismatch])
    assert mismatched_socket.close_calls == [(1008, "event context mismatch")]
    assert mismatched_factory.sessions[0].finish_calls == [True]

    matching = json.loads(json.dumps(mismatch))
    matching["hangup"]["callId"] = "call-1"
    _, _, matching_socket, matching_factory = await run_gateway([START, matching])
    assert matching_socket.close_calls == [(1000, "call ended")]
    assert matching_factory.sessions[0].finish_calls == [True]


@pytest.mark.asyncio
async def test_gateway_disconnect_is_normal_and_cleanup_is_exactly_once():
    disconnect = WebSocketDisconnect(code=1000)
    _, registry, websocket, factory = await run_gateway([START, disconnect])

    assert websocket.close_calls == []
    assert factory.sessions[0].finish_calls == [True]
    assert factory.sessions[0].transport.is_active is False
    assert registry.snapshot()["active_sessions"] == 0
    assert registry.snapshot()["released_total"] == 1


@pytest.mark.asyncio
async def test_gateway_idle_timeout_finishes_session_and_releases_slot():
    settings = replace(
        SmartPBXSettings.from_env(enabled_environment()),
        idle_timeout_seconds=0.01,
    )
    _, registry, websocket, factory = await run_gateway([START], settings=settings)

    assert websocket.close_calls == [(1008, "idle timeout")]
    assert factory.sessions[0].finish_calls == [True]
    assert factory.sessions[0].transport.is_active is False
    assert registry.snapshot()["active_sessions"] == 0


@pytest.mark.asyncio
async def test_gateway_sends_only_documented_outbound_media_envelope():
    factory = SessionFactory(audio_on_start=b"\x7f\x80")
    _, _, websocket, _ = await run_gateway(
        [START, {"event": "stop"}], factory=factory,
    )

    assert [json.loads(item) for item in websocket.sent_text] == [{
        "event": "media",
        "callId": "call-1",
        "accountId": "account-1",
        "media": {"payload": base64.b64encode(b"\x7f\x80").decode("ascii")},
    }]


@pytest.mark.asyncio
async def test_gateway_unexpected_session_failure_closes_generically_and_releases():
    async def broken_factory(context, transport):
        raise RuntimeError("sensitive provider details")

    _, registry, websocket, _ = await run_gateway([START], factory=broken_factory)

    assert websocket.close_calls == [(1011, "internal error")]
    assert "sensitive" not in repr(websocket.close_calls)
    assert registry.snapshot()["active_sessions"] == 0


@pytest.mark.asyncio
async def test_gateway_cleanup_logs_never_include_exception_details(caplog):
    class CleanupFailureSession(FakeSession):
        async def finish(self, schedule_post_call=False):
            self.finish_calls.append(schedule_post_call)
            raise RuntimeError("sensitive caller +15550000001 and call-1")

    class CleanupFailureFactory:
        def __init__(self):
            self.sessions = []

        async def __call__(self, context, transport):
            session = CleanupFailureSession(context, transport)
            self.sessions.append(session)
            return session

    caplog.set_level("INFO", logger="smartpbx_gateway")
    factory = CleanupFailureFactory()
    _, registry, websocket, _ = await run_gateway(
        [START, {"event": "stop"}], factory=factory,
    )

    log_output = caplog.text
    assert factory.sessions[0].finish_calls == [True]
    assert registry.snapshot()["active_sessions"] == 0
    assert websocket.close_calls == [(1000, "call ended")]
    assert "+15550000001" not in log_output
    assert "call-1" not in log_output
    assert "sensitive caller" not in log_output
    assert "session_cleanup" in log_output
