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


def decoded_media_frames(websocket):
    """Decode fake-WebSocket payloads only for outbound byte-order assertions."""
    return [
        base64.b64decode(json.loads(message)["media"]["payload"])
        for message in websocket.sent
    ]


ULAW_640 = bytes(range(256)) * 2 + bytes(range(128))
ULAW_SILENCE_FRAME = b"\xff" * 160


@pytest.mark.asyncio
async def test_startup_preroll_sends_five_full_silence_frames_and_waits_for_the_wire():
    websocket = FakeWebSocket()
    transport = SmartPBXMediaTransport(websocket, CONTEXT, max_queue_frames=8)
    transport.start()
    try:
        assert await transport.send_startup_preroll(5) is True

        assert decoded_media_frames(websocket) == [ULAW_SILENCE_FRAME] * 5
    finally:
        await transport.close()


@pytest.mark.asyncio
async def test_startup_preroll_is_claimed_before_await_so_concurrent_calls_send_once():
    websocket = FakeWebSocket()
    websocket.allow_send.clear()
    transport = SmartPBXMediaTransport(websocket, CONTEXT, max_queue_frames=8)
    transport.start()
    first = asyncio.create_task(transport.send_startup_preroll(5))
    try:
        await asyncio.wait_for(websocket.send_started.wait(), timeout=1)
        second = asyncio.create_task(transport.send_startup_preroll(5))
        websocket.allow_send.set()
        results = await asyncio.wait_for(asyncio.gather(first, second), timeout=1)

        assert results == [True, False]
        assert decoded_media_frames(websocket) == [ULAW_SILENCE_FRAME] * 5
    finally:
        websocket.allow_send.set()
        await transport.close()


@pytest.mark.asyncio
async def test_startup_preroll_has_no_turn_bound_delivery_or_first_media_events():
    websocket = FakeWebSocket()
    events = []
    transport = SmartPBXMediaTransport(
        websocket, CONTEXT, max_queue_frames=8,
        on_transport_event=lambda *event: events.append(event),
    )
    transport.start()
    try:
        assert await transport.send_startup_preroll(5) is True

        assert events == []
        assert transport.cadence_summary_for_generation(transport.generation) == {
            "frames_160b": 0,
            "frames_partial": 0,
            "frames_other": 0,
            "inter_send_gap_p95_ms": 0,
            "inter_send_gap_max_ms": 0,
            "queue_underruns": 0,
        }
    finally:
        await transport.close()


@pytest.mark.asyncio
async def test_real_turn_after_startup_preroll_keeps_its_own_first_media_and_cadence():
    websocket = FakeWebSocket()
    events = []
    transport = SmartPBXMediaTransport(
        websocket,
        CONTEXT,
        max_queue_frames=8,
        on_transport_event=lambda turn_id, generation, stage, *_: events.append(
            (turn_id, generation, stage)
        ),
    )
    transport.start()
    real_frame = b"\xa5" * 160
    try:
        assert await transport.send_startup_preroll(5) is True
        transport.bind_turn(transport.generation, "turn-1")
        await transport.send_audio(real_frame)
        await transport.send_mark("turn-1-done")

        assert decoded_media_frames(websocket) == [ULAW_SILENCE_FRAME] * 5 + [real_frame]
        assert events == [
            ("turn-1", 0, "first_media_sent"),
            ("turn-1", 0, "queue_drained"),
        ]
        assert transport.cadence_summary_for_generation(transport.generation) == {
            "frames_160b": 1,
            "frames_partial": 0,
            "frames_other": 0,
            "inter_send_gap_p95_ms": 0,
            "inter_send_gap_max_ms": 0,
            "queue_underruns": 0,
        }
    finally:
        await transport.close()


@pytest.mark.asyncio
async def test_startup_preroll_returns_false_when_its_generation_changes_mid_enqueue():
    class GenerationChangingTransport(SmartPBXMediaTransport):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self._cleared = False

        async def _enqueue_frame(self, audio, generation, *, startup_preroll=False):
            accepted = await super()._enqueue_frame(
                audio, generation, startup_preroll=startup_preroll
            )
            if accepted and not self._cleared:
                self._cleared = True
                await self.clear_audio()
            return accepted

    websocket = FakeWebSocket()
    transport = GenerationChangingTransport(websocket, CONTEXT, max_queue_frames=8)
    transport.start()
    try:
        assert await transport.send_startup_preroll(5) is False
        assert decoded_media_frames(websocket) == []
    finally:
        await transport.close()


@pytest.mark.asyncio
async def test_reframes_a_640_byte_mulaw_chunk_into_four_ordered_20ms_frames():
    websocket = FakeWebSocket()
    transport = SmartPBXMediaTransport(websocket, CONTEXT, max_queue_frames=8)
    transport.start()
    try:
        await transport.send_audio(ULAW_640)
        await asyncio.wait_for(wait_for_sent(websocket, 4), timeout=1)
        await transport.send_mark("tts_done")

        assert decoded_media_frames(websocket) == [
            ULAW_640[offset:offset + 160] for offset in range(0, 640, 160)
        ]
    finally:
        await transport.close()


@pytest.mark.asyncio
async def test_reframes_fragmented_mulaw_input_without_reordering_bytes():
    websocket = FakeWebSocket()
    transport = SmartPBXMediaTransport(websocket, CONTEXT, max_queue_frames=8)
    transport.start()
    try:
        await transport.send_audio(ULAW_640[:40])
        await transport.send_audio(ULAW_640[40:160])
        await transport.send_audio(ULAW_640[160:])
        await asyncio.wait_for(wait_for_sent(websocket, 4), timeout=1)
        await transport.send_mark("tts_done")

        assert decoded_media_frames(websocket) == [
            ULAW_640[offset:offset + 160] for offset in range(0, 640, 160)
        ]
    finally:
        await transport.close()


@pytest.mark.asyncio
async def test_send_mark_pads_the_final_mulaw_residual_to_a_20ms_silence_frame():
    websocket = FakeWebSocket()
    transport = SmartPBXMediaTransport(websocket, CONTEXT, max_queue_frames=8)
    transport.start()
    audio = ULAW_640 + b"\xfe"
    try:
        await transport.send_audio(audio)
        await asyncio.wait_for(wait_for_sent(websocket, 4), timeout=1)

        assert decoded_media_frames(websocket) == [
            audio[offset:offset + 160] for offset in range(0, 640, 160)
        ]

        await transport.send_mark("tts_done")
        assert decoded_media_frames(websocket) == [
            audio[offset:offset + 160] for offset in range(0, 640, 160)
        ] + [b"\xfe" + ULAW_SILENCE_FRAME[1:]]
    finally:
        await transport.close()


@pytest.mark.asyncio
async def test_clear_audio_discards_a_live_generation_residual_before_it_can_send():
    websocket = FakeWebSocket()
    transport = SmartPBXMediaTransport(websocket, CONTEXT, max_queue_frames=8)
    transport.start()
    try:
        await transport.send_audio(b"\xa1" * 159)
        await asyncio.sleep(0.01)
        await transport.clear_audio()
        await transport.send_audio(b"\xb2" * 160)
        await asyncio.wait_for(wait_for_sent(websocket, 1), timeout=1)
        await transport.send_mark("tts_done")

        assert decoded_media_frames(websocket) == [b"\xb2" * 160]
    finally:
        await transport.close()


@pytest.mark.asyncio
async def test_clear_during_final_residual_enqueue_discards_the_stale_padded_frame():
    class BlockingResidualTransport(SmartPBXMediaTransport):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self.enqueue_entered = asyncio.Event()
            self.resume_enqueue = asyncio.Event()

        async def _enqueue_frame(self, audio, generation):
            self.enqueue_entered.set()
            await self.resume_enqueue.wait()
            return await super()._enqueue_frame(audio, generation)

    websocket = FakeWebSocket()
    transport = BlockingResidualTransport(websocket, CONTEXT, max_queue_frames=8)
    transport.start()
    try:
        await transport.send_audio(b"\xd4" * 159)
        marker = asyncio.create_task(transport.send_mark("tts_done"))
        await asyncio.wait_for(transport.enqueue_entered.wait(), timeout=1)
        await transport.clear_audio()
        transport.resume_enqueue.set()
        await asyncio.wait_for(marker, timeout=1)

        await transport.send_audio(b"\xe5" * 160)
        await asyncio.wait_for(wait_for_sent(websocket, 1), timeout=1)
        await transport.send_mark("current")
        assert decoded_media_frames(websocket) == [b"\xe5" * 160]
    finally:
        transport.resume_enqueue.set()
        await transport.close()


@pytest.mark.asyncio
async def test_close_discards_a_live_generation_residual_without_a_partial_wire_frame():
    websocket = FakeWebSocket()
    transport = SmartPBXMediaTransport(websocket, CONTEXT, max_queue_frames=8)
    transport.start()
    await transport.send_audio(b"\xc3" * 159)
    await asyncio.sleep(0.01)
    await transport.close()

    assert decoded_media_frames(websocket) == []


@pytest.mark.asyncio
async def test_sends_only_the_documented_media_envelope():
    websocket = FakeWebSocket()
    transport = SmartPBXMediaTransport(websocket, CONTEXT, max_queue_frames=2)
    transport.start()
    await transport.send_audio(b"audio")
    await transport.send_mark("tts_done")
    await asyncio.wait_for(wait_for_sent(websocket, 1), timeout=1)

    assert json.loads(websocket.sent[0]) == {
        "event": "media", "callId": "call-1", "accountId": "account-1",
        "media": {
            "payload": base64.b64encode(b"audio" + ULAW_SILENCE_FRAME[5:]).decode("ascii")
        },
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
    in_flight = b"i" * 160
    oldest = b"o" * 160
    newest = b"n" * 160
    latest = b"l" * 160
    await transport.send_audio(in_flight)
    await asyncio.wait_for(websocket.send_started.wait(), timeout=1)
    await transport.send_audio(oldest)
    await transport.send_audio(newest)
    await transport.send_audio(latest)
    websocket.allow_send.set()
    await asyncio.wait_for(wait_for_sent(websocket, 3), timeout=1)

    assert [json.loads(item)["media"]["payload"] for item in websocket.sent] == [
        base64.b64encode(audio).decode("ascii") for audio in (in_flight, oldest, newest)
    ]
    assert transport.frames_dropped_total == 1
    await transport.close()


@pytest.mark.asyncio
async def test_clear_audio_discards_stale_generation_without_a_wire_control_event():
    websocket = FakeWebSocket()
    websocket.allow_send.clear()
    transport = SmartPBXMediaTransport(websocket, CONTEXT, max_queue_frames=2)
    transport.start()
    in_flight = b"i" * 160
    current = b"c" * 160
    await transport.send_audio(in_flight)
    await asyncio.wait_for(websocket.send_started.wait(), timeout=1)
    await transport.send_audio(b"s" * 160)
    await transport.clear_audio()
    await transport.send_audio(current)
    websocket.allow_send.set()
    await asyncio.wait_for(wait_for_sent(websocket, 2), timeout=1)

    assert [json.loads(item)["media"]["payload"] for item in websocket.sent] == [
        base64.b64encode(audio).decode("ascii") for audio in (in_flight, current)
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
    await transport.send_audio(b"i" * 160)
    await asyncio.wait_for(websocket.send_started.wait(), timeout=1)
    await transport.send_audio(b"a" * 160)
    await transport.send_audio(b"b" * 160)
    await transport.clear_audio()
    assert transport.frames_dropped_total == 0
    await transport.close()


@pytest.mark.asyncio
async def test_send_audio_backpressures_and_bails_on_barge_in_without_hanging():
    websocket = FakeWebSocket()
    websocket.allow_send.clear()
    transport = SmartPBXMediaTransport(websocket, CONTEXT, max_queue_frames=1)
    transport.start()
    await transport.send_audio(b"i" * 160)  # taken by the (blocked) sender
    await asyncio.wait_for(websocket.send_started.wait(), timeout=1)
    await transport.send_audio(b"q" * 160)     # fills the 1-slot queue

    # A further frame must backpressure (wait for space), not drop immediately.
    pending = asyncio.create_task(transport.send_audio(b"w" * 160))
    await asyncio.sleep(0.01)
    assert not pending.done(), "a full queue must backpressure, not drop the tail at once"
    assert transport.frames_dropped_total == 0

    # A barge-in supersedes the generation: the waiting frame is abandoned
    # cleanly (uncounted) and does not hang.
    await transport.clear_audio()
    await asyncio.wait_for(pending, timeout=1)
    assert transport.frames_dropped_total == 0
    await transport.close()


@pytest.mark.asyncio
async def test_reframed_overflow_keeps_a_contiguous_prefix_and_counts_partial_tail_loss():
    """A saturated queue may cut the reply short, but may not reorder its bytes."""

    class SlowPayloadSocket(FakeWebSocket):
        async def send_text(self, message):
            # Exceeds the bounded 200 ms admission wait, forcing drop-newest.
            await asyncio.sleep(0.4)
            await super().send_text(message)

    websocket = SlowPayloadSocket()
    transport = SmartPBXMediaTransport(websocket, CONTEXT, max_queue_frames=2)
    source = b"".join(bytes([index]) * 160 for index in range(20)) + b"tail"
    transport.start()
    try:
        await transport.send_audio(source)
        await transport.send_mark("tts_done")
        delivered = b"".join(decoded_media_frames(websocket))

        assert delivered == source[:len(delivered)]
        assert transport.frames_dropped_total > 0
        assert delivered != source, "the sustained overload must drop the newest tail"
    finally:
        await transport.close()


@pytest.mark.asyncio
async def test_cadence_accounting_uses_fixed_buckets_and_detects_one_open_utterance_underrun():
    websocket = FakeWebSocket()
    events: list[str] = []
    transport = SmartPBXMediaTransport(
        websocket,
        CONTEXT,
        max_queue_frames=8,
        on_transport_event=lambda _turn_id, _generation, stage, *_: events.append(stage),
    )
    transport.bind_turn(0, "opaque-turn")
    transport.start()
    try:
        await transport.send_audio(b"\x7f" * 160)
        await asyncio.wait_for(wait_for_sent(websocket, 1), timeout=1)
        # The generation remains open without a marker, so its next scheduled
        # 20 ms send instant is a real sender underrun rather than a terminal gap.
        await asyncio.sleep(0.035)

        assert transport.cadence_summary_for_generation(0) == {
            "frames_160b": 1,
            "frames_partial": 0,
            "frames_other": 0,
            "inter_send_gap_p95_ms": 0,
            "inter_send_gap_max_ms": 0,
            "queue_underruns": 1,
        }
        assert events.count("queue_underrun") == 1
    finally:
        await transport.close()


@pytest.mark.asyncio
async def test_marker_clear_and_close_are_not_cadence_underruns():
    async def stages_after_terminal(action):
        websocket = FakeWebSocket()
        stages: list[str] = []
        transport = SmartPBXMediaTransport(
            websocket,
            CONTEXT,
            max_queue_frames=8,
            on_transport_event=lambda _turn_id, _generation, stage, *_: stages.append(stage),
        )
        transport.bind_turn(0, "opaque-turn")
        transport.start()
        await transport.send_audio(b"\x4d" * 160)
        await asyncio.wait_for(wait_for_sent(websocket, 1), timeout=1)
        await action(transport)
        await asyncio.sleep(0.035)
        if transport.is_active:
            await transport.close()
        return stages

    assert "queue_underrun" not in await stages_after_terminal(
        lambda transport: transport.send_mark("tts_done")
    )
    assert "queue_underrun" not in await stages_after_terminal(
        lambda transport: transport.clear_audio()
    )
    assert "queue_underrun" not in await stages_after_terminal(
        lambda transport: transport.close()
    )


@pytest.mark.asyncio
async def test_normal_turn_reuse_emits_fresh_first_send_and_drain():
    """A normal next turn must not need a barge-in to reset transport telemetry."""
    websocket = FakeWebSocket()
    events: list[tuple[str, int, str]] = []
    transport = SmartPBXMediaTransport(
        websocket,
        CONTEXT,
        max_queue_frames=8,
        on_transport_event=lambda turn_id, generation, stage, *_: events.append(
            (turn_id, generation, stage)
        ),
    )
    transport.start()
    try:
        transport.bind_turn(0, "turn-one")
        await transport.send_audio(b"\x10" * 160)
        await transport.send_mark("turn-one-done")

        # The reply completed normally, so Dialog has not cleared audio and the
        # live transport generation is intentionally still zero.
        transport.bind_turn(0, "turn-two")
        await transport.send_audio(b"\x20" * 160)
        await transport.send_mark("turn-two-done")

        assert events.count(("turn-one", 0, "first_media_sent")) == 1
        assert events.count(("turn-one", 0, "queue_drained")) == 1
        assert events.count(("turn-two", 0, "first_media_sent")) == 1
        assert events.count(("turn-two", 0, "queue_drained")) == 1
    finally:
        await transport.close()


@pytest.mark.asyncio
async def test_clear_and_close_retire_generation_cadence_state():
    """Transport keeps only live/needed cadence state, never an unbounded history."""
    websocket = FakeWebSocket()
    transport = SmartPBXMediaTransport(websocket, CONTEXT, max_queue_frames=8)
    transport.start()
    try:
        transport.bind_turn(0, "turn-one")
        await transport.send_audio(b"\x30" * 160)
        await transport.send_mark("turn-one-done")
        await transport.clear_audio()

        assert transport._generation_turn_ids == {}
        assert transport._cadence_by_generation == {}
        assert transport._residual_by_generation == {}
        assert transport._queued_frames_by_generation == {}
    finally:
        await transport.close()

    assert transport._generation_turn_ids == {}
    assert transport._cadence_by_generation == {}
    assert transport._residual_by_generation == {}
    assert transport._queued_frames_by_generation == {}
