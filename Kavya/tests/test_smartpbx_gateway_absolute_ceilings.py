"""Audit #2: transfer-pending/idle ceilings must be absolute, not per-message.

Before this fix, `_receive_or_terminal` computed a fresh full timeout on every
call and awaited a brand-new receive task each time; as long as inbound Dialog
media frames (or, for the new hard cap, any inbound message at all) kept
arriving faster than the configured window, the ceiling never actually
elapsed -- pinning the registry slot for the life of the call. These tests
drive that exact scenario with an injected monotonic clock so the ceiling's
absolute nature is provable without a real multi-minute wait.
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

MEDIA = {"event": "media", "media": {"payload": "AAAA"}}


def settings(**overrides):
    base = SmartPBXSettings.from_env({
        "ENABLE_SMARTPBX_WSS": "true", "SMARTPBX_WS_TOKEN": "token",
        "SMARTPBX_ACCOUNT_ID": "account-1",
    })
    return replace(base, **overrides)


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
    def __init__(self, *, transfer_pending: bool = False):
        self.transfer_pending = transfer_pending
        self.terminal_future = asyncio.get_running_loop().create_future()
        self.finishes = 0
        self.last_close_reason = None
        self.last_close_code = None

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


class _FakeClock:
    """A monotonic clock that jumps forward by `step` on every read.

    Standing in for the real several-minutes-of-wall-clock elapse a genuine
    reproduction of the audit bug would otherwise require.
    """

    def __init__(self, step: float, start: float = 0.0):
        self._t = start
        self._step = step

    def __call__(self) -> float:
        self._t += self._step
        return self._t


@pytest.mark.asyncio
async def test_transfer_pending_ceiling_is_absolute_despite_continuous_media_frames():
    """Regression for audit #2. Old behavior: a fresh receive_task per
    iteration meant every inbound media frame re-entered _receive_or_terminal
    and restarted the 300s clock, so a carrier that kept streaming the
    pending leg's audio pinned the slot forever. New behavior: the ceiling is
    measured from the moment transfer_pending first became true and fires
    regardless of how many messages keep arriving.
    """
    session = Session(transfer_pending=True)
    socket = Socket()
    # Far more media frames than should ever be consumed before the ceiling
    # fires -- if the old per-message-reset bug were still present, the
    # gateway would keep draining this queue forever (bounded here only by
    # the outer asyncio.wait_for so the test itself cannot hang).
    for _ in range(200):
        socket.messages.put_nowait(json.dumps(MEDIA))

    gateway = SmartPBXGateway(
        settings(idle_timeout_seconds=10, transfer_pending_timeout_seconds=100),
        SmartPBXSessionRegistry(4),
        clock=_FakeClock(step=40.0),
    )
    await asyncio.wait_for(gateway.handle(socket, _factory(session)), timeout=5)

    assert session.finishes == 1
    assert (session.last_close_reason, session.last_close_code) == (
        "transfer_pending_timeout", POLICY_VIOLATION,
    )
    assert socket.close_calls == [(POLICY_VIOLATION, "transfer timeout")]
    # Fewer than the full 200 queued frames were drained -- proof the ceiling
    # cut the call off rather than running the queue dry.
    assert socket.messages.qsize() > 0


@pytest.mark.asyncio
async def test_max_call_seconds_is_a_hard_ceiling_despite_continuous_activity():
    """SMARTPBX_MAX_CALL_SECONDS: a per-call maximum independent of both
    idleness and transfer state. A call that never idles and never transfers
    must still end eventually."""
    session = Session(transfer_pending=False)
    socket = Socket()
    for _ in range(200):
        socket.messages.put_nowait(json.dumps(MEDIA))

    gateway = SmartPBXGateway(
        settings(idle_timeout_seconds=90, transfer_pending_timeout_seconds=300, max_call_seconds=300),
        SmartPBXSessionRegistry(4),
        clock=_FakeClock(step=100.0),
    )
    await asyncio.wait_for(gateway.handle(socket, _factory(session)), timeout=5)

    assert session.finishes == 1
    assert (session.last_close_reason, session.last_close_code) == ("max_call_duration", 1000)
    # Unlike a timeout/protocol violation, the ceiling closes politely.
    assert socket.close_calls == [(1000, "call ended")]
    assert socket.messages.qsize() > 0


@pytest.mark.asyncio
async def test_max_call_seconds_still_bounds_a_pending_transfer():
    """The hard cap must win even over a transfer that is legitimately still
    within its own (much longer) pending ceiling."""
    session = Session(transfer_pending=True)
    socket = Socket()
    for _ in range(200):
        socket.messages.put_nowait(json.dumps(MEDIA))

    gateway = SmartPBXGateway(
        settings(idle_timeout_seconds=10, transfer_pending_timeout_seconds=1800, max_call_seconds=300),
        SmartPBXSessionRegistry(4),
        clock=_FakeClock(step=50.0),
    )
    await asyncio.wait_for(gateway.handle(socket, _factory(session)), timeout=5)

    assert session.finishes == 1
    assert session.last_close_reason == "max_call_duration"


@pytest.mark.asyncio
async def test_max_call_seconds_setting_defaults_bounds_and_env_tunable():
    assert settings().max_call_seconds == 3600

    assert settings(max_call_seconds=300).max_call_seconds == 300
    assert settings(max_call_seconds=7200).max_call_seconds == 7200

    for rejected in ("0", "299", "7201", "-1", "", "abc", "3600.5"):
        with pytest.raises(ValueError):
            SmartPBXSettings.from_env({
                "ENABLE_SMARTPBX_WSS": "true", "SMARTPBX_WS_TOKEN": "token",
                "SMARTPBX_ACCOUNT_ID": "account-1",
                "SMARTPBX_MAX_CALL_SECONDS": rejected,
            })


@pytest.mark.asyncio
async def test_ordinary_short_calls_are_unaffected_by_the_new_ceiling():
    """Sanity check with the real clock: a normal short call still ends on
    hangup/stop exactly as before, nowhere near either ceiling."""
    session = Session(transfer_pending=False)
    socket = Socket()
    socket.messages.put_nowait(json.dumps({"event": "stop"}))

    gateway = SmartPBXGateway(settings(), SmartPBXSessionRegistry(4))
    await asyncio.wait_for(gateway.handle(socket, _factory(session)), timeout=5)

    assert session.finishes == 1
    assert (session.last_close_reason, session.last_close_code) == ("stop", 1000)
