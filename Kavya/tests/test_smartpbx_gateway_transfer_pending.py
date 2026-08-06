"""Gateway must leave an acknowledged Dialog transfer open for carrier events."""

from __future__ import annotations

import asyncio
import json
from dataclasses import replace

import pytest

from smartpbx_gateway import SmartPBXGateway, SmartPBXSessionRegistry, SmartPBXSettings


START = {"event": "start", "start": {
    "callId": "call-1", "otherLegCallId": "other-1",
    "callerIdNumber": "caller", "calleeIdNumber": "callee", "accountId": "account-1",
    "mediaFormat": {"encoding": "g711_ulaw", "sampleRate": 8000},
}}


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

    async def finish(self, schedule_post_call=False):
        self.finishes += int(schedule_post_call)


@pytest.mark.asyncio
async def test_transfer_pending_waits_for_dialog_terminal_event_past_normal_idle_timeout():
    settings = replace(SmartPBXSettings.from_env({
        "ENABLE_SMARTPBX_WSS": "true", "SMARTPBX_WS_TOKEN": "token",
        "SMARTPBX_ACCOUNT_ID": "account-1",
    }), idle_timeout_seconds=0.01)
    session = Session()

    async def factory(_context, _transport):
        return session

    socket = Socket()
    gateway = SmartPBXGateway(settings, SmartPBXSessionRegistry(4))
    task = asyncio.create_task(gateway.handle(socket, factory))
    await asyncio.sleep(0.03)
    assert not task.done(), "ordinary AI idleness must not close an acknowledged transfer"
    socket.messages.put_nowait(json.dumps({"event": "stop"}))
    await task

    assert socket.close_calls == [(1000, "call ended")]
    assert session.finishes == 1
