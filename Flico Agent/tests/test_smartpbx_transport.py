"""Contract tests for bounded outbound Dialog SmartPBX media."""

import asyncio
import base64
import json

import pytest

from smartpbx_protocol import CallContext, MediaFormat
from smartpbx_transport import SmartPBXMediaTransport


CONTEXT = CallContext(
    call_id="call-main",
    other_leg_call_id="call-other-leg",
    caller_id_number="+94110000000",
    callee_id_number="+94114698850",
    account_id="account-1",
    media_format=MediaFormat(encoding="g711_ulaw", sample_rate=8000),
)


class FakeWebSocket:
    def __init__(self):
        self.sent = []
        self.send_started = asyncio.Event()
        self.allow_send = asyncio.Event()
        self.allow_send.set()

    async def send_text(self, message):
        self.send_started.set()
        await self.allow_send.wait()
        self.sent.append(message)


async def wait_for_sent(websocket, count):
    await asyncio.wait_for(_has_sent(websocket, count), timeout=1)


async def _has_sent(websocket, count):
    while len(websocket.sent) < count:
        await asyncio.sleep(0)


@pytest.mark.asyncio
async def test_sends_the_exact_smartpbx_media_envelope():
    ws = FakeWebSocket()
    transport = SmartPBXMediaTransport(ws, CONTEXT, max_queue_frames=2)
    transport.start()

    await transport.send_audio(b"audio")
    await wait_for_sent(ws, 1)

    assert json.loads(ws.sent[0]) == {
        "event": "media",
        "callId": "call-main",
        "accountId": "account-1",
        "media": {"payload": base64.b64encode(b"audio").decode("ascii")},
    }
    await transport.close()


@pytest.mark.asyncio
async def test_one_sender_preserves_queued_audio_order():
    ws = FakeWebSocket()
    transport = SmartPBXMediaTransport(ws, CONTEXT, max_queue_frames=3)
    transport.start()

    for audio in (b"one", b"two", b"three"):
        await transport.send_audio(audio)
    await wait_for_sent(ws, 3)

    assert [json.loads(message)["media"]["payload"] for message in ws.sent] == [
        base64.b64encode(audio).decode("ascii") for audio in (b"one", b"two", b"three")
    ]
    await transport.close()


@pytest.mark.asyncio
async def test_overflow_drops_oldest_queued_audio_without_blocking():
    ws = FakeWebSocket()
    transport = SmartPBXMediaTransport(ws, CONTEXT, max_queue_frames=2)
    transport.start()

    await transport.send_audio(b"oldest")
    await transport.send_audio(b"middle")
    await transport.send_audio(b"newest")
    await wait_for_sent(ws, 2)

    assert [json.loads(message)["media"]["payload"] for message in ws.sent] == [
        base64.b64encode(audio).decode("ascii") for audio in (b"middle", b"newest")
    ]
    await transport.close()


@pytest.mark.asyncio
async def test_clear_audio_drops_queued_stale_frames_without_a_wire_event():
    ws = FakeWebSocket()
    transport = SmartPBXMediaTransport(ws, CONTEXT, max_queue_frames=2)
    transport.start()

    await transport.send_audio(b"stale")
    await transport.clear_audio()
    await transport.send_audio(b"current")
    await wait_for_sent(ws, 1)

    assert [json.loads(message)["media"]["payload"] for message in ws.sent] == [
        base64.b64encode(b"current").decode("ascii")
    ]
    await transport.close()


@pytest.mark.asyncio
async def test_send_mark_has_no_wire_event_and_clears_speaking_acknowledgement():
    ws = FakeWebSocket()
    transport = SmartPBXMediaTransport(ws, CONTEXT, max_queue_frames=2)
    transport.start()
    transport._speaking_acknowledged = True

    await transport.send_mark("speech-finished")

    assert ws.sent == []
    assert transport.is_speaking_acknowledged is False
    await transport.close()


@pytest.mark.asyncio
async def test_close_cancels_an_inflight_sender_and_marks_transport_inactive():
    ws = FakeWebSocket()
    ws.allow_send.clear()
    transport = SmartPBXMediaTransport(ws, CONTEXT, max_queue_frames=2)
    transport.start()
    await transport.send_audio(b"blocked")
    await asyncio.wait_for(ws.send_started.wait(), timeout=1)

    await transport.close()

    assert transport.is_active is False
    assert ws.sent == []


@pytest.mark.asyncio
async def test_sends_after_close_are_noops_and_close_is_idempotent():
    ws = FakeWebSocket()
    transport = SmartPBXMediaTransport(ws, CONTEXT, max_queue_frames=2)
    transport.start()
    await transport.close()

    await transport.send_audio(b"ignored")
    await transport.send_mark("ignored")
    await transport.clear_audio()
    await transport.close()

    assert ws.sent == []
    assert transport.is_active is False
