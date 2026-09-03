"""An acknowledged Dialog transfer waits past normal idleness, but not forever.

`transfer_pending` is never reset and ACKNOWLEDGED is terminal, so a transfer
whose carrier terminal event never arrives used to pin its capacity slot for the
life of the container. Four of those stop the hotel receiving calls. The wait is
therefore generous but bounded, and the ceiling ends the call cleanly.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import replace

import pytest

from smartpbx_gateway import SmartPBXGateway, SmartPBXSessionRegistry, SmartPBXSettings
from smartpbx_protocol import POLICY_VIOLATION


START = {"event": "start", "start": {
    "callId": "call-1", "otherLegCallId": "other-1",
    "callerIdNumber": "caller", "calleeIdNumber": "callee", "accountId": "account-1",
    "mediaFormat": {"encoding": "g711_ulaw", "sampleRate": 8000},
}}


def settings(**overrides):
    base = SmartPBXSettings.from_env({
        "ENABLE_SMARTPBX_WSS": "true", "SMARTPBX_WS_TOKEN": "token",
        "SMARTPBX_ACCOUNT_ID": "account-1",
    })
    return replace(base, **overrides)


def diagnostics(caplog):
    records = []
    for record in caplog.records:
        try:
            payload = json.loads(record.getMessage())
        except ValueError:
            continue
        if payload.get("event") == "smartpbx_protocol_diagnostic":
            records.append(payload)
    return records


class Socket:
    def __init__(self):
        self.headers = {"X-Kavya-SmartPBX-Token": "token"}
        self.messages: asyncio.Queue[str] = asyncio.Queue()
        self.messages.put_nowait(json.dumps(START))
        self.accepted = False
        self.close_calls = []

    async def accept(self):
        self.accepted = True

    async def receive_text(self):
        return await self.messages.get()

    async def close(self, code=1000, reason=""):
        self.close_calls.append((code, reason))

    async def send_text(self, _message):
        pass


class Session:
    def __init__(self):
        self.transfer_pending = True
        self.terminal_future = asyncio.get_running_loop().create_future()
        self.finishes = 0

    async def start(self):
        pass

    async def feed_audio(self, _audio):
        pass

    async def finish(self, schedule_post_call=False, close_reason=None, close_code=None):
        self.finishes += int(schedule_post_call)
        self.last_close_reason = close_reason
        self.last_close_code = close_code


def _factory(session):
    async def factory(_context, _transport, sink=None):
        assert sink is None or callable(sink)
        return session
    return factory


@pytest.mark.asyncio
async def test_transfer_pending_waits_for_dialog_terminal_event_past_normal_idle_timeout():
    session = Session()
    socket = Socket()
    gateway = SmartPBXGateway(
        settings(idle_timeout_seconds=0.01, transfer_pending_timeout_seconds=30),
        SmartPBXSessionRegistry(4),
    )
    task = asyncio.create_task(gateway.handle(socket, _factory(session)))
    await asyncio.sleep(0.03)
    assert not task.done(), "ordinary AI idleness must not close an acknowledged transfer"
    socket.messages.put_nowait(json.dumps({"event": "stop"}))
    await task

    assert socket.close_calls == [(1000, "call ended")]
    assert session.finishes == 1


@pytest.mark.asyncio
async def test_transfer_becoming_pending_during_receive_rechecks_the_idle_deadline():
    session = Session()
    session.transfer_pending = False
    socket = Socket()
    gateway = SmartPBXGateway(
        settings(idle_timeout_seconds=0.03, transfer_pending_timeout_seconds=30),
        SmartPBXSessionRegistry(4),
    )
    task = asyncio.create_task(gateway.handle(socket, _factory(session)))
    await asyncio.sleep(0.01)
    session.transfer_pending = True
    await asyncio.sleep(0.04)
    assert not task.done()
    socket.messages.put_nowait(json.dumps({"event": "hangup"}))
    await task
    assert session.finishes == 1


@pytest.mark.asyncio
async def test_transfer_pending_without_a_terminal_event_releases_the_slot(caplog):
    session = Session()
    socket = Socket()
    registry = SmartPBXSessionRegistry(4)
    gateway = SmartPBXGateway(
        settings(idle_timeout_seconds=10, transfer_pending_timeout_seconds=0.05), registry
    )
    with caplog.at_level("INFO"):
        await asyncio.wait_for(gateway.handle(socket, _factory(session)), timeout=5)

    assert registry.snapshot()["active_sessions"] == 0, (
        "a transfer whose terminal event never arrives must not pin its capacity "
        "slot; four stuck transfers would stop the hotel receiving calls"
    )
    assert session.finishes == 1
    assert socket.close_calls, "the ceiling must end the call, not just stop waiting"
    failures = [record["failure_class"] for record in diagnostics(caplog)]
    assert "transfer_pending_timeout" in failures, (
        f"the ceiling needs its own diagnostic, not idle_timeout: {failures}"
    )
    assert "idle_timeout" not in failures
    assert (session.last_close_reason, session.last_close_code) == (
        "transfer_pending_timeout", POLICY_VIOLATION,
    )


@pytest.mark.asyncio
async def test_transfer_pending_ceiling_is_bounded_and_env_tunable():
    assert SmartPBXSettings.from_env({
        "ENABLE_SMARTPBX_WSS": "true", "SMARTPBX_WS_TOKEN": "token",
        "SMARTPBX_ACCOUNT_ID": "account-1",
    }).transfer_pending_timeout_seconds == 300

    tuned = SmartPBXSettings.from_env({
        "ENABLE_SMARTPBX_WSS": "true", "SMARTPBX_WS_TOKEN": "token",
        "SMARTPBX_ACCOUNT_ID": "account-1",
        "SMARTPBX_TRANSFER_PENDING_TIMEOUT_SECONDS": "120",
    })
    assert tuned.transfer_pending_timeout_seconds == 120

    # A blank compose passthrough means "absent" and resolves to the default.
    assert SmartPBXSettings.from_env({
        "ENABLE_SMARTPBX_WSS": "true", "SMARTPBX_WS_TOKEN": "token",
        "SMARTPBX_ACCOUNT_ID": "account-1", "SMARTPBX_TRANSFER_PENDING_TIMEOUT_SECONDS": "",
    }).transfer_pending_timeout_seconds == 300
    for rejected in ("0", "29", "1801", "-1", "abc", "300.5"):
        with pytest.raises(ValueError):
            SmartPBXSettings.from_env({
                "ENABLE_SMARTPBX_WSS": "true", "SMARTPBX_WS_TOKEN": "token",
                "SMARTPBX_ACCOUNT_ID": "account-1",
                "SMARTPBX_TRANSFER_PENDING_TIMEOUT_SECONDS": rejected,
            })
