"""Contract tests for Kavya SmartPBX admission and lifecycle boundaries."""

import asyncio
import logging
import base64
import json
import re

from smartpbx_diagnostics import DiagnosticFailureClass, DiagnosticOutcome, DiagnosticStage
from smartpbx_protocol import POLICY_VIOLATION, ProtocolViolation
from dataclasses import replace

from starlette.websockets import WebSocketDisconnect
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
        self.sinks = []
        self.ready = asyncio.Event()

    async def __call__(self, context, transport, sink=None):
        self.sinks.append(sink)
        session = FakeSession(context, transport)
        self.sessions.append(session)
        self.ready.set()
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
    assert (configuration.max_message_chars, configuration.max_audio_bytes, configuration.max_outbound_frames) == (65536, 32768, 512)
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
    await asyncio.wait_for(factory.ready.wait(), timeout=.2)
    factory.sessions[0].terminal_future.set_result(None)
    await task

    assert socket.close_calls == [(1000, "call ended")]
    assert factory.sessions[0].finishes == [True]


@pytest.mark.asyncio
async def test_gateway_supplies_a_non_null_three_enum_diagnostic_sink_to_the_factory():
    _, _, _, factory = await run([START, {"event": "stop"}])

    assert len(factory.sinks) == 1
    assert callable(factory.sinks[0])


def test_gateway_status_and_protocol_export_have_no_unknown_event_counter_or_alias():
    import smartpbx_protocol

    gateway = SmartPBXGateway(settings(), SmartPBXSessionRegistry(4))

    assert "unknown_events_total" not in gateway.snapshot()
    assert not hasattr(smartpbx_protocol, "UnknownEvent")


def test_unknown_counter_removal_is_independent_from_alias_removal():
    gateway = SmartPBXGateway(settings(), SmartPBXSessionRegistry(4))
    assert "unknown_events_total" not in gateway.snapshot()


def test_unknown_alias_removal_is_independent_from_counter_removal():
    import smartpbx_protocol
    assert not hasattr(smartpbx_protocol, "UnknownEvent")


@pytest.mark.parametrize("event", ["future-event", "another-future-event"])
@pytest.mark.asyncio
async def test_gateway_rejects_unsupported_events_before_start(event):
    _, _, socket, factory = await run([{"event": event}, START, {"event": "stop"}])
    assert socket.close_calls == [(1008, "unsupported event")]
    assert factory.sessions == []


@pytest.mark.parametrize("field", ["callId", "otherLegCallId"])
@pytest.mark.asyncio
async def test_gateway_rejects_dtmf_for_either_mismatched_leg(field):
    dtmf = {"event": "dtmf", "dtmf": {"callId": "call-1", "otherLegCallId": "other-1", "digit": "5"}}
    dtmf["dtmf"][field] = "different"
    _, _, socket, factory = await run([START, dtmf, {"event": "stop"}])
    assert socket.close_calls == [(1000, "call ended")]
    assert factory.sessions[0].context.call_id == "call-1"


@pytest.mark.asyncio
async def test_gateway_does_not_teardown_for_unknown_events_after_start():
    _, _, socket, _ = await run([START, {"event": "future-event"}, {"event": "stop"}])
    assert socket.close_calls == [(1000, "call ended")]


@pytest.mark.asyncio
async def test_gateway_first_terminal_leaves_later_frames_unread_and_cleans_once():
    hangup = {"event": "hangup", "hangup": {"callId": "call-1", "otherLegCallId": "other-1", "accountId": "account-1", "reason": "normal"}}
    later = {"event": "media", "media": {"payload": "YQ=="}}
    _, registry, socket, factory = await run([START, hangup, later, {"event": "stop"}])
    assert factory.sessions[0].finishes == [True]
    assert socket.messages == [later, {"event": "stop"}]
    assert socket.close_calls == [(1000, "call ended")]
    assert registry.snapshot()["released_total"] == 1


DIAGNOSTIC_KEYS = {
    "event", "correlation_id", "stage", "outcome",
    "failure_class", "active_sessions", "duration_ms",
}


def fixed_diagnostics(caplog):
    records = []
    for record in caplog.records:
        try:
            payload = json.loads(record.getMessage())
        except (TypeError, json.JSONDecodeError):
            continue
        if payload.get("event") == "smartpbx_protocol_diagnostic":
            assert set(payload) == DIAGNOSTIC_KEYS
            assert re.fullmatch(r"spx-[0-9a-f]{32}", payload["correlation_id"])
            assert payload["stage"] in {item.value for item in DiagnosticStage}
            assert payload["outcome"] in {item.value for item in DiagnosticOutcome}
            assert payload["failure_class"] in {item.value for item in DiagnosticFailureClass}
            assert type(payload["active_sessions"]) is int and 0 <= payload["active_sessions"] <= 4
            assert type(payload["duration_ms"]) is int and payload["duration_ms"] >= 0
            records.append(payload)
    return records


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "messages,configuration,token,expected",
    [
        ([], SmartPBXSettings.from_env({"ENABLE_SMARTPBX_WSS": "false"}), "test-token",
         ("schema_admission", "rejected", "disabled")),
        ([], None, "wrong-token", ("schema_admission", "rejected", "authentication")),
        (['{'], None, "test-token", ("schema_admission", "rejected", "invalid_message")),
        ([START, {"event": "stop"}], None, "test-token",
         [("session_start", "completed", "none"), ("terminal_cleanup", "completed", "none")]),
        ([START, {"event": "dtmf", "dtmf": {"callId": "call-1", "otherLegCallId": "other-1", "digit": "5"}}, {"event": "stop"}],
         None, "test-token", [("session_start", "completed", "none"), ("context_validation", "observed", "none"), ("terminal_cleanup", "completed", "none")]),
    ],
)
async def test_gateway_emits_exact_fixed_diagnostic_rows(
    caplog, messages, configuration, token, expected,
):
    with caplog.at_level("INFO"):
        await run(messages, configuration=configuration, token=token)
    records = fixed_diagnostics(caplog)
    expected_rows = expected if isinstance(expected, list) else [expected]
    assert [(record.get("stage"), record.get("outcome"), record.get("failure_class")) for record in records] == expected_rows
    assert all(set(record) == DIAGNOSTIC_KEYS for record in records)


@pytest.mark.asyncio
async def test_gateway_diagnostic_record_has_exact_keys_and_no_live_sentinels(caplog):
    start = json.loads(json.dumps(START))
    start["start"].update({"callId": "call-sentinel", "otherLegCallId": "other-sentinel", "callerIdNumber": "caller-sentinel", "accountId": "account-sentinel"})
    with caplog.at_level("INFO"):
        await run([start, {"event": "stop"}], configuration=settings(SMARTPBX_ACCOUNT_ID="account-sentinel"))
    records = fixed_diagnostics(caplog)
    assert len(records) == 2
    assert all(set(record) == DIAGNOSTIC_KEYS for record in records)
    rendered = caplog.text
    for sentinel in ("call-sentinel", "other-sentinel", "caller-sentinel", "account-sentinel", "test-token", "session_id", "call_fingerprint"):
        assert sentinel not in rendered


@pytest.mark.asyncio
async def test_gateway_four_calls_have_unique_opaque_correlations(caplog):
    with caplog.at_level("INFO"):
        results = await asyncio.gather(*(run([START, {"event": "stop"}]) for _ in range(4)))
    assert all(result[1].snapshot()["released_total"] == 1 for result in results)
    records = fixed_diagnostics(caplog)
    correlations = [record["correlation_id"] for record in records]
    assert len(correlations) == 8
    assert len(set(correlations)) == 4
    assert all(correlations.count(value) == 2 for value in set(correlations))
    assert all(__import__("re").fullmatch(r"spx-[0-9a-f]{32}", value) for value in correlations)


class _FaultTransport:
    def __init__(self, _websocket, _context, *, max_queue_frames, on_frame_dropped=None):
        self.close_calls = 0
        self.fail = _FAULT_STATE["transport"]
        self._send_failed = asyncio.Event()
    def start(self):
        pass
    async def wait_send_failed(self):
        await self._send_failed.wait()
    async def close(self):
        self.close_calls += 1
        _FAULT_STATE["order"].append("transport")
        if self.fail:
            raise RuntimeError("transport-sentinel")


class _FaultSession(FakeSession):
    def __init__(self, context, transport):
        super().__init__(context, transport)
        self.finish_started = asyncio.Event()
    async def finish(self, schedule_post_call=False):
        self.finishes.append(schedule_post_call)
        self.finish_started.set()
        _FAULT_STATE["order"].append("session")
        if _FAULT_STATE["block_finish"]:
            await _FAULT_STATE["release_finish"].wait()
        if _FAULT_STATE["session"]:
            raise RuntimeError("session-sentinel")


class _FaultFactory:
    def __init__(self):
        self.sessions = []
        self.sinks = []
        self.ready = asyncio.Event()
    async def __call__(self, context, transport, sink=None):
        self.sinks.append(sink)
        session = _FaultSession(context, transport)
        self.sessions.append(session)
        self.ready.set()
        return session


class _FaultLease:
    def __init__(self, inner):
        self.inner = inner
        self.calls = 0
    async def release(self):
        self.calls += 1
        _FAULT_STATE["order"].append("lease")
        await self.inner.release()
        if _FAULT_STATE["lease"]:
            raise RuntimeError("lease-sentinel")


def assert_late_fault_sink_is_disabled(caplog, factory):
    sink = factory.sinks[0]
    assert callable(sink), "gateway must supply a typed diagnostic sink before late-noop behavior is testable"
    before_records = len(caplog.records)
    before_text = caplog.text
    sink(
        DiagnosticStage.TERMINAL_CLEANUP,
        DiagnosticOutcome.COMPLETED,
        DiagnosticFailureClass.NONE,
    )
    assert len(caplog.records) == before_records
    assert caplog.text == before_text


@pytest.mark.asyncio
@pytest.mark.parametrize("fault", ["session", "transport", "lease"])
async def test_gateway_cleanup_faults_continue_once_and_require_fixed_degraded_tuple(monkeypatch, caplog, fault):
    import smartpbx_gateway as gateway_module
    global _FAULT_STATE
    _FAULT_STATE = {"session": fault == "session", "transport": fault == "transport",
                    "lease": fault == "lease", "order": [], "block_finish": False,
                    "release_finish": asyncio.Event()}
    monkeypatch.setattr(gateway_module, "SmartPBXMediaTransport", _FaultTransport)
    registry = SmartPBXSessionRegistry(4)
    original_acquire = registry.try_acquire
    async def acquire():
        return _FaultLease(await original_acquire())
    monkeypatch.setattr(registry, "try_acquire", acquire)
    socket = FakeWebSocket([START, {"event": "stop"}])
    factory = _FaultFactory()
    with caplog.at_level("INFO"):
        await SmartPBXGateway(settings(), registry).handle(socket, factory)
    assert _FAULT_STATE["order"] == ["transport", "session", "lease"]
    assert factory.sessions[0].finishes == [True]
    assert registry.snapshot()["released_total"] == 1
    assert socket.close_calls == [(1000, "call ended")]
    assert_late_fault_sink_is_disabled(caplog, factory)
    assert [(r["stage"], r["outcome"], r["failure_class"]) for r in fixed_diagnostics(caplog)] == [
        ("session_start", "completed", "none"),
        ("terminal_cleanup", "degraded", fault + "_cleanup"),
    ]


@pytest.mark.asyncio
async def test_gateway_cancellation_shields_cleanup_then_reraises_and_disables_late_sink(monkeypatch, caplog):
    import smartpbx_gateway as gateway_module
    global _FAULT_STATE
    _FAULT_STATE = {"session": False, "transport": False, "lease": False, "order": [],
                    "block_finish": True, "release_finish": asyncio.Event()}
    monkeypatch.setattr(gateway_module, "SmartPBXMediaTransport", _FaultTransport)
    registry = SmartPBXSessionRegistry(4)
    original_acquire = registry.try_acquire
    async def acquire():
        return _FaultLease(await original_acquire())
    monkeypatch.setattr(registry, "try_acquire", acquire)
    socket = FakeWebSocket([START, {"event": "stop"}])
    factory = _FaultFactory()
    caplog.set_level(logging.INFO)
    task = asyncio.create_task(SmartPBXGateway(settings(), registry).handle(socket, factory))
    try:
        await asyncio.wait_for(factory.ready.wait(), timeout=.2)
        await asyncio.wait_for(factory.sessions[0].finish_started.wait(), timeout=.2)
        task.cancel()
        _FAULT_STATE["release_finish"].set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert _FAULT_STATE["order"] == ["transport", "session", "lease"]
        assert registry.snapshot()["released_total"] == 1
        assert socket.close_calls == [(1000, "call ended")]
        assert_late_fault_sink_is_disabled(caplog, factory)
        assert [(r["stage"], r["outcome"], r["failure_class"]) for r in fixed_diagnostics(caplog)] == [
            ("session_start", "completed", "none"),
            ("terminal_cleanup", "cancelled", "cancelled"),
        ]
    finally:
        _FAULT_STATE["release_finish"].set()
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)


class _CloseFaultWebSocket(FakeWebSocket):
    def __init__(self, messages):
        super().__init__(messages)
        self.close_attempts = 0
    async def close(self, code=1000, reason=""):
        self.close_attempts += 1
        self.close_calls.append((code, reason))
        raise RuntimeError("close-sentinel")


@pytest.mark.asyncio
async def test_gateway_completed_call_is_not_degraded_when_the_peer_already_closed(caplog):
    # A clean hangup: the caller (or carrier) closes the socket, so our courtesy
    # close raises. The call still COMPLETED — it must not be labelled degraded,
    # or every normal call (successful bookings included) reports a false fault
    # and the diagnostic can no longer flag a real problem.
    registry = SmartPBXSessionRegistry(4)
    socket = _CloseFaultWebSocket([START, {"event": "stop"}])
    factory = Factory()
    with caplog.at_level("INFO"):
        await SmartPBXGateway(settings(), registry).handle(socket, factory)
    assert factory.sessions[0].finishes == [True]
    assert registry.snapshot()["released_total"] == 1
    assert socket.close_attempts == 1, "teardown behaviour is unchanged: close is still attempted once"
    assert socket.close_calls == [(1000, "call ended")]
    assert_late_fault_sink_is_disabled(caplog, factory)
    assert [(r["stage"], r["outcome"], r["failure_class"]) for r in fixed_diagnostics(caplog)] == [
        ("session_start", "completed", "none"),
        ("terminal_cleanup", "completed", "none"),
    ]


@pytest.mark.asyncio
async def test_gateway_close_fault_on_an_uncompleted_call_is_still_degraded(caplog):
    # A close that fails on a call that did NOT complete cleanly is a genuine
    # degradation and must still be flagged.
    registry = SmartPBXSessionRegistry(4)
    socket = _CloseFaultWebSocket([START, {"event": "future-event"}])
    factory = Factory()
    with caplog.at_level("INFO"):
        await SmartPBXGateway(settings(), registry).handle(socket, factory)
    assert socket.close_attempts == 1
    tuples = [(r["stage"], r["outcome"], r["failure_class"]) for r in fixed_diagnostics(caplog)]
    assert ("terminal_cleanup", "degraded", "websocket_close") in tuples
    assert ("terminal_cleanup", "completed", "none") not in tuples


@pytest.mark.asyncio
async def test_gateway_late_captured_sink_is_noop_after_handle_completion(caplog):
    from smartpbx_diagnostics import (
        DiagnosticFailureClass, DiagnosticOutcome, DiagnosticStage,
    )
    with caplog.at_level("INFO"):
        _, _, _, factory = await run([START, {"event": "stop"}])
    sink = factory.sinks[0]
    assert callable(sink), "gateway must supply a typed diagnostic sink before late-noop behavior is testable"
    before_records = len(caplog.records)
    before_text = caplog.text
    sink(
        DiagnosticStage.TERMINAL_CLEANUP,
        DiagnosticOutcome.COMPLETED,
        DiagnosticFailureClass.NONE,
    )
    assert len(caplog.records) == before_records
    assert caplog.text == before_text
@pytest.mark.parametrize(
    "messages,configuration,expected",
    [
        ([{"event": "media", "media": {"payload": "YQ=="}}], None, ("schema_admission", "rejected", "start_required")),
        ([{**START, "start": {**START["start"], "accountId": "other"}}], None, ("context_validation", "rejected", "account_mismatch")),
        ([START, {"event": "connected"}], None, [("session_start", "completed", "none"), ("context_validation", "rejected", "connected_after_start")]),
        ([START, {"event": "start", "start": START["start"]}], None, [("session_start", "completed", "none"), ("context_validation", "rejected", "duplicate_start")]),
        ([START, {"event": "media", "media": {"payload": "bad"}}], None, [("session_start", "completed", "none"), ("schema_admission", "rejected", "invalid_media")]),
        ([START, {"event": "dtmf", "dtmf": {"callId": "call-1", "otherLegCallId": "other-1", "digit": "Z"}}], None, [("session_start", "completed", "none"), ("schema_admission", "rejected", "invalid_dtmf")]),
    ],
)
@pytest.mark.asyncio
async def test_decision_rows_emit_exact_fixed_diagnostic(caplog, messages, configuration, expected):
    caplog.set_level(logging.INFO)
    await run(messages, configuration=configuration)
    rows = fixed_diagnostics(caplog)
    assert rows
    assert all(set(row) == DIAGNOSTIC_KEYS for row in rows)
    assert [(row.get("stage"), row.get("outcome"), row.get("failure_class")) for row in rows] == (expected if isinstance(expected, list) else [expected])
@pytest.mark.asyncio
async def test_idle_timeout_emits_audio_ingestion_diagnostic(caplog):
    caplog.set_level(logging.INFO)
    await run([START], configuration=replace(settings(), idle_timeout_seconds=0.01))
    rows = fixed_diagnostics(caplog)
    assert [(row.get("stage"), row.get("outcome"), row.get("failure_class")) for row in rows] == [("session_start", "completed", "none"), ("audio_ingestion", "failed", "idle_timeout")]
@pytest.mark.asyncio
async def test_message_too_big_emits_schema_diagnostic(caplog):
    caplog.set_level(logging.INFO)
    await run(["{" + "x" * 64], configuration=replace(settings(), max_message_chars=16))
    rows = fixed_diagnostics(caplog)
    assert [(row.get("stage"), row.get("outcome"), row.get("failure_class")) for row in rows] == [("schema_admission", "rejected", "message_too_big")]
@pytest.mark.asyncio
async def test_capacity_emits_schema_admission_diagnostic(caplog):
    caplog.set_level(logging.INFO)
    configuration = settings()
    registry = SmartPBXSessionRegistry(4)
    leases = [await registry.try_acquire() for _ in range(4)]
    try:
        await run([], configuration=configuration, registry=registry)
        rows = fixed_diagnostics(caplog)
        assert [(row.get("stage"), row.get("outcome"), row.get("failure_class")) for row in rows] == [("schema_admission", "rejected", "capacity")]
    finally:
        for lease in leases:
            await lease.release()
@pytest.mark.asyncio
async def test_unsupported_media_format_emits_schema_admission_diagnostic(caplog):
    caplog.set_level(logging.INFO)
    bad = {**START, "start": {**START["start"], "mediaFormat": {"encoding": "pcm16", "sampleRate": 8000}}}
    await run([bad])
    rows = fixed_diagnostics(caplog)
    assert [(row.get("stage"), row.get("outcome"), row.get("failure_class")) for row in rows] == [("schema_admission", "rejected", "unsupported_media_format")]
@pytest.mark.asyncio
async def test_audio_too_big_emits_schema_admission_diagnostic(caplog):
    caplog.set_level(logging.INFO)
    payload = base64.b64encode(b"abc").decode("ascii")
    await run([START, {"event": "media", "media": {"payload": payload}}], configuration=replace(settings(), max_audio_bytes=2))
    rows = fixed_diagnostics(caplog)
    assert [(row.get("stage"), row.get("outcome"), row.get("failure_class")) for row in rows] == [("session_start", "completed", "none"), ("schema_admission", "rejected", "audio_too_big")]
@pytest.mark.asyncio
async def test_start_timeout_emits_schema_admission_diagnostic(caplog):
    caplog.set_level(logging.INFO)
    await run([], configuration=replace(settings(), start_timeout_seconds=0.01))
    rows = fixed_diagnostics(caplog)
    assert [(row.get("stage"), row.get("outcome"), row.get("failure_class")) for row in rows] == [("schema_admission", "rejected", "start_timeout")]
class _LifecycleSession:
    def __init__(self, *, start_error=None, feed_error=None):
        import asyncio
        self.start_error = start_error
        self.feed_error = feed_error
        self.terminal_future = asyncio.get_running_loop().create_future()
        self.finishes = []
    async def start(self):
        if self.start_error:
            raise self.start_error
    async def feed_audio(self, _audio):
        if self.feed_error:
            raise self.feed_error
    async def finish(self, schedule_post_call=False):
        self.finishes.append(schedule_post_call)
class _LifecycleFactory:
    def __init__(self, *, factory_error=None, start_error=None, feed_error=None):
        self.factory_error, self.start_error, self.feed_error = factory_error, start_error, feed_error
        self.session_ready = asyncio.Event()
        self.session = None
    async def __call__(self, _context, _transport, sink=None):
        if self.factory_error:
            raise self.factory_error
        self.session = _LifecycleSession(start_error=self.start_error, feed_error=self.feed_error)
        self.session_ready.set()
        return self.session
async def _run_lifecycle(messages, factory):
    registry = SmartPBXSessionRegistry(4)
    socket = FakeWebSocket(messages)
    await SmartPBXGateway(settings(), registry).handle(socket, factory)
    return registry, socket, factory
@pytest.mark.asyncio
@pytest.mark.parametrize(
    "factory,messages,expected",
    [
        (_LifecycleFactory(factory_error=RuntimeError("factory")), [START], ("session_start", "failed", "session_factory")),
        (_LifecycleFactory(start_error=RuntimeError("start")), [START], ("session_start", "failed", "session_start")),
        (_LifecycleFactory(feed_error=RuntimeError("feed")), [START, {"event": "media", "media": {"payload": "YQ=="}}], [("session_start", "completed", "none"), ("audio_ingestion", "failed", "audio_ingestion")]),
        (_LifecycleFactory(), [START, {"event": "stop"}], [("session_start", "completed", "none"), ("terminal_cleanup", "completed", "none")]),
        (_LifecycleFactory(), [START, {"event": "hangup", "hangup": {"callId": "call-1", "otherLegCallId": "other-1", "accountId": "account-1", "reason": "normal"}}], [("session_start", "completed", "none"), ("terminal_cleanup", "completed", "none")]),
        (_LifecycleFactory(), [START, WebSocketDisconnect()], [("session_start", "completed", "none"), ("terminal_cleanup", "disconnected", "transport_disconnect")]),
    ],
)
async def test_lifecycle_decision_rows_are_exact_and_cleanup_deterministically(caplog, factory, messages, expected):
    caplog.set_level(logging.INFO)
    registry, socket, factory = await _run_lifecycle(messages, factory)
    rows = fixed_diagnostics(caplog)
    assert [(row.get("stage"), row.get("outcome"), row.get("failure_class")) for row in rows] == (expected if isinstance(expected, list) else [expected])
    assert registry.snapshot()["active_sessions"] == 0
    if factory.session is not None:
        assert factory.session.finishes == [True]
@pytest.mark.asyncio
async def test_terminal_future_completion_emits_terminal_cleanup_row(caplog):
    caplog.set_level(logging.INFO)
    factory = _LifecycleFactory()
    registry = SmartPBXSessionRegistry(4)
    socket = FakeWebSocket([START])
    task = asyncio.create_task(SmartPBXGateway(settings(), registry).handle(socket, factory))
    try:
        await asyncio.wait_for(factory.session_ready.wait(), timeout=.2)
        factory.session.terminal_future.set_result(None)
        await asyncio.wait_for(task, timeout=.2)
        rows = fixed_diagnostics(caplog)
        assert [(row.get("stage"), row.get("outcome"), row.get("failure_class")) for row in rows] == [("session_start", "completed", "none"), ("terminal_cleanup", "completed", "none")]
        assert registry.snapshot()["released_total"] == 1
    finally:
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)

class _AcceptFailureWebSocket(FakeWebSocket):
    async def accept(self):
        raise RuntimeError("accept-sentinel")
@pytest.mark.asyncio
async def test_unexpected_accept_failure_emits_terminal_internal_error_tuple(caplog):
    caplog.set_level(logging.INFO)
    registry = SmartPBXSessionRegistry(4)
    await SmartPBXGateway(settings(), registry).handle(_AcceptFailureWebSocket([]), _LifecycleFactory())
    assert [(row.get("stage"), row.get("outcome"), row.get("failure_class")) for row in fixed_diagnostics(caplog)] == [
        ("terminal_cleanup", "failed", "internal_error")
    ]
    assert registry.snapshot()["active_sessions"] == 0

@pytest.mark.asyncio
@pytest.mark.parametrize("messages,expected", [
    ([{"event": "future-event"}, START, {"event": "stop"}], [("schema_admission", "rejected", "unsupported_event")]),
    ([START, {"event": "future-event"}, {"event": "stop"}], [("session_start", "completed", "none"), ("context_validation", "observed", "unsupported_event"), ("terminal_cleanup", "completed", "none")]),
    ([START, {"event": "dtmf", "dtmf": {"callId": "wrong", "otherLegCallId": "other-1", "digit": "5"}}, {"event": "stop"}], [("session_start", "completed", "none"), ("context_validation", "observed", "context_mismatch"), ("terminal_cleanup", "completed", "none")]),
    ([START, {"event": "hangup", "hangup": {"callId": "wrong", "otherLegCallId": "other-1", "accountId": "account-1", "reason": "normal"}}], [("session_start", "completed", "none"), ("context_validation", "rejected", "context_mismatch")]),
])
async def test_explicit_unsupported_and_context_mismatch_sequences(caplog, messages, expected):
    caplog.set_level(logging.INFO)
    await run(messages)
    assert [(r.get("stage"), r.get("outcome"), r.get("failure_class")) for r in fixed_diagnostics(caplog)] == expected


@pytest.mark.asyncio
async def test_unknown_protocol_violation_maps_to_internal_error_without_dynamic_class(monkeypatch, caplog):
    import smartpbx_gateway as module
    original = module.parse_smartpbx_event
    calls = 0
    def parse(raw, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            return original(raw, **kwargs)
        raise ProtocolViolation(POLICY_VIOLATION, "safe", "unrecognized_class")
    monkeypatch.setattr(module, "parse_smartpbx_event", parse)
    caplog.set_level(logging.INFO)
    await run([START, {"event": "stop"}])
    assert [(r.get("stage"), r.get("outcome"), r.get("failure_class")) for r in fixed_diagnostics(caplog)] == [
        ("session_start", "completed", "none"), ("schema_admission", "failed", "internal_error")
    ]
    assert "unrecognized_class" not in caplog.text


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["unsupported_name", "invalid_media", "audio_too_big", "factory_exception"])
async def test_gateway_never_leaks_raw_or_derived_values_in_any_log_record(monkeypatch, caplog, kind):
    sentinels = (
        "event-name-sentinel", "payload-sentinel", "audio-sentinel",
        "exception-sentinel", "call-sentinel", "account-sentinel",
        "caller-sentinel", "test-token", "session_id", "call_fingerprint",
    )
    if kind == "unsupported_name":
        messages = [{"event": "event-name-sentinel"}, START, {"event": "stop"}]
        invoke = lambda: run(messages)
    elif kind == "invalid_media":
        messages = [START, {"event": "media", "media": {"payload": "payload-sentinel-audio-sentinel"}}]
        invoke = lambda: run(messages)
    elif kind == "audio_too_big":
        raw_payload = base64.b64encode(b"audio-sentinel").decode("ascii")
        sentinels = (*sentinels, raw_payload)
        messages = [START, {"event": "media", "media": {"payload": raw_payload}}]
        invoke = lambda: run(messages, configuration=replace(settings(), max_audio_bytes=1))
    else:
        class RaisingFactory:
            async def __call__(self, _context, _transport, sink=None):
                raise RuntimeError("exception-sentinel")
        async def invoke_factory():
            registry = SmartPBXSessionRegistry(4)
            socket = FakeWebSocket([START])
            await SmartPBXGateway(settings(), registry).handle(socket, RaisingFactory())
        invoke = invoke_factory
    with caplog.at_level("INFO"):
        await invoke()
    fixed_diagnostics(caplog)
    assert all(value not in caplog.text for value in sentinels)


class _BlockingCloseWebSocket(FakeWebSocket):
    def __init__(self, messages):
        super().__init__(messages)
        self.close_attempts = 0
        self.close_started = asyncio.Event()
        self.release_close = asyncio.Event()

    async def close(self, code=1000, reason=""):
        self.close_attempts += 1
        self.close_calls.append((code, reason))
        self.close_started.set()
        await self.release_close.wait()


@pytest.mark.asyncio
async def test_gateway_cancellation_during_blocking_close_reraises_and_disables_sink(caplog):
    registry = SmartPBXSessionRegistry(4)
    socket = _BlockingCloseWebSocket([START, {"event": "stop"}])
    factory = Factory()
    caplog.set_level(logging.INFO)
    task = asyncio.create_task(SmartPBXGateway(settings(), registry).handle(socket, factory))
    try:
        await asyncio.wait_for(socket.close_started.wait(), timeout=.2)
        task.cancel()
        socket.release_close.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert socket.close_attempts == 1
        assert socket.close_calls == [(1000, "call ended")]
        assert factory.sessions[0].finishes == [True]
        assert registry.snapshot()["released_total"] == 1
        assert [(record["stage"], record["outcome"], record["failure_class"]) for record in fixed_diagnostics(caplog)] == [
            ("session_start", "completed", "none"),
            ("terminal_cleanup", "cancelled", "cancelled"),
        ]
        sink = factory.sinks[0]
        before_records = len(caplog.records)
        before_text = caplog.text
        sink(
            DiagnosticStage.TERMINAL_CLEANUP,
            DiagnosticOutcome.COMPLETED,
            DiagnosticFailureClass.NONE,
        )
        assert len(caplog.records) == before_records
        assert caplog.text == before_text
    finally:
        socket.release_close.set()
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)


class _DtmfSession(FakeSession):
    def __init__(self, context, transport):
        super().__init__(context, transport)
        self.dtmf_digits: list[str] = []

    async def feed_dtmf(self, digit):
        self.dtmf_digits.append(digit)
        return True


class _DtmfFactory(Factory):
    async def __call__(self, context, transport, sink=None):
        self.sinks.append(sink)
        session = _DtmfSession(context, transport)
        self.sessions.append(session)
        self.ready.set()
        return session


@pytest.mark.asyncio
async def test_gateway_routes_dtmf_to_the_session_and_still_observes(caplog):
    dtmf = {"event": "dtmf", "dtmf": {"callId": "call-1", "otherLegCallId": "other-1", "digit": "7"}}
    registry = SmartPBXSessionRegistry(4)
    socket = FakeWebSocket([START, dtmf, {"event": "stop"}], token="test-token", header="X-Kavya-SmartPBX-Token")
    factory = _DtmfFactory()
    with caplog.at_level("INFO"):
        await SmartPBXGateway(settings(), registry).handle(socket, factory)

    assert factory.sessions[0].dtmf_digits == ["7"], "the digit must reach the session collector hook"
    tuples = [(r["stage"], r["outcome"], r["failure_class"]) for r in fixed_diagnostics(caplog)]
    assert ("context_validation", "observed", "none") in tuples, "DTMF observability must be preserved"


@pytest.mark.asyncio
async def test_gateway_dtmf_without_a_collector_hook_still_observes(caplog):
    # FakeSession has no feed_dtmf; the gateway must fall back to OBSERVED cleanly.
    dtmf = {"event": "dtmf", "dtmf": {"callId": "call-1", "otherLegCallId": "other-1", "digit": "5"}}
    registry = SmartPBXSessionRegistry(4)
    socket = FakeWebSocket([START, dtmf, {"event": "stop"}], token="test-token", header="X-Kavya-SmartPBX-Token")
    factory = Factory()
    with caplog.at_level("INFO"):
        await SmartPBXGateway(settings(), registry).handle(socket, factory)

    tuples = [(r["stage"], r["outcome"], r["failure_class"]) for r in fixed_diagnostics(caplog)]
    assert ("context_validation", "observed", "none") in tuples
