"""Contract tests for bounded outbound Dialog SmartPBX media."""

import asyncio
import base64
import json

import pytest

from smartpbx_protocol import CallContext, MediaFormat
from smartpbx_transport import SmartPBXMediaTransport


CONTEXT = CallContext(
    call_id="call-1", other_leg_call_id="other-1", caller_id_number="caller-opaque",
    callee_id_number="callee-opaque", account_id="account-1",
    media_format=MediaFormat("g711_ulaw", 8000),
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
    while len(websocket.sent) < count:
        await asyncio.sleep(0)


@pytest.mark.asyncio
async def test_sends_only_the_documented_media_envelope():
    websocket = FakeWebSocket()
    transport = SmartPBXMediaTransport(websocket, CONTEXT, max_queue_frames=2)
    transport.start()
    await transport.send_audio(b"audio")
    await asyncio.wait_for(wait_for_sent(websocket, 1), timeout=1)

    assert json.loads(websocket.sent[0]) == {
        "event": "media", "callId": "call-1", "accountId": "account-1",
        "media": {"payload": base64.b64encode(b"audio").decode("ascii")},
    }
    await transport.close()


@pytest.mark.asyncio
async def test_serialized_sender_drops_newest_when_bounded_queue_overflows():
    """Overflow must truncate the tail, not decimate what is already queued.

    Evicting the oldest frame discards the one that was next due, so the guest
    hears playback jump forward again and again. Refusing the newest frame keeps
    the delivered audio a contiguous prefix and simply ends it early.
    """
    websocket = FakeWebSocket()
    websocket.allow_send.clear()
    transport = SmartPBXMediaTransport(websocket, CONTEXT, max_queue_frames=2)
    transport.start()
    await transport.send_audio(b"in-flight")
    await asyncio.wait_for(websocket.send_started.wait(), timeout=1)
    await transport.send_audio(b"oldest")
    await transport.send_audio(b"newest")
    await transport.send_audio(b"latest")
    websocket.allow_send.set()
    await asyncio.wait_for(wait_for_sent(websocket, 3), timeout=1)

    assert [json.loads(item)["media"]["payload"] for item in websocket.sent] == [
        base64.b64encode(audio).decode("ascii") for audio in (b"in-flight", b"oldest", b"newest")
    ]
    assert transport.frames_dropped_total == 1
    await transport.close()


@pytest.mark.asyncio
async def test_clear_audio_discards_stale_generation_without_a_wire_control_event():
    websocket = FakeWebSocket()
    websocket.allow_send.clear()
    transport = SmartPBXMediaTransport(websocket, CONTEXT, max_queue_frames=2)
    transport.start()
    await transport.send_audio(b"in-flight")
    await asyncio.wait_for(websocket.send_started.wait(), timeout=1)
    await transport.send_audio(b"stale")
    await transport.clear_audio()
    await transport.send_audio(b"current")
    websocket.allow_send.set()
    await asyncio.wait_for(wait_for_sent(websocket, 2), timeout=1)

    assert [json.loads(item)["media"]["payload"] for item in websocket.sent] == [
        base64.b64encode(audio).decode("ascii") for audio in (b"in-flight", b"current")
    ]
    await transport.close()


@pytest.mark.asyncio
async def test_close_is_idempotent_and_later_operations_are_noops():
    websocket = FakeWebSocket()
    transport = SmartPBXMediaTransport(websocket, CONTEXT, max_queue_frames=2)
    transport.start()
    await transport.close()
    await transport.send_audio(b"ignored")
    await transport.send_mark("ignored")
    await transport.clear_audio()
    await transport.close()

    assert websocket.sent == []
    assert transport.is_active is False


@pytest.mark.asyncio
async def test_clear_audio_drain_is_not_counted_as_a_drop():
    # Only a genuine full-queue overflow counts; clearing must not inflate the
    # frames_dropped_total metric (the 504-in-one-call evidence is real overflow).
    websocket = FakeWebSocket()
    websocket.allow_send.clear()
    transport = SmartPBXMediaTransport(websocket, CONTEXT, max_queue_frames=4)
    transport.start()
    await transport.send_audio(b"in-flight")
    await asyncio.wait_for(websocket.send_started.wait(), timeout=1)
    await transport.send_audio(b"a")
    await transport.send_audio(b"b")
    await transport.clear_audio()
    assert transport.frames_dropped_total == 0
    await transport.close()


@pytest.mark.asyncio
async def test_send_audio_backpressures_and_bails_on_barge_in_without_hanging():
    websocket = FakeWebSocket()
    websocket.allow_send.clear()
    transport = SmartPBXMediaTransport(websocket, CONTEXT, max_queue_frames=1)
    transport.start()
    await transport.send_audio(b"in-flight")  # taken by the (blocked) sender
    await asyncio.wait_for(websocket.send_started.wait(), timeout=1)
    await transport.send_audio(b"queued")     # fills the 1-slot queue

    # A further frame must backpressure (wait for space), not drop immediately.
    pending = asyncio.create_task(transport.send_audio(b"waiting"))
    await asyncio.sleep(0.01)
    assert not pending.done(), "a full queue must backpressure, not drop the tail at once"
    assert transport.frames_dropped_total == 0

    # A barge-in supersedes the generation: the waiting frame is abandoned
    # cleanly (uncounted) and does not hang.
    await transport.clear_audio()
    await asyncio.wait_for(pending, timeout=1)
    assert transport.frames_dropped_total == 0
    await transport.close()
