"""Bounded outbound media transport for Dialog SmartPBX calls."""

from __future__ import annotations

import asyncio
import base64
import contextlib
import json
import math
import time
from dataclasses import dataclass, field
from typing import Any, Callable

from smartpbx_protocol import CallContext


# g711_ulaw at 8 kHz carries one byte per sample, so a frame's wall-clock
# duration is simply its length over this rate.
_ULAW_BYTES_PER_SECOND = 8000
# Client Connect's reference provider sends g711_ulaw@8000 in 20 ms packets.
# At one byte/sample that is exactly 160 bytes.  Reframing belongs here, before
# the bounded queue, so the queue still reasons about wire frames.
_ULAW_FRAME_BYTES = 160
_MAX_CADENCE_GAPS = 256
# Grace window a full outbound queue waits for the realtime sender to drain
# before it gives up and drops the newest frame. Bursty TTS outruns realtime
# pacing, so a short wait turns most would-be overflow drops into backpressure
# while the hard drop-newest remains the backstop for sustained overload.
_SEND_BACKPRESSURE_SECONDS = 0.2
_SEND_BACKPRESSURE_POLL = 0.005

# Matches the gateway's saturating admission counters.
_MAX_COUNTER = (1 << 63) - 1


@dataclass(frozen=True)
class _QueuedAudio:
    generation: int
    audio: bytes


@dataclass
class _CadenceState:
    """Bounded, payload-free outbound wire-cadence accounting."""

    frames_160b: int = 0
    frames_partial: int = 0
    frames_other: int = 0
    inter_send_gaps_ms: list[int] = field(default_factory=list)
    previous_send_ns: int | None = None
    queue_underruns: int = 0
    utterance_open: bool = False

class SmartPBXMediaTransport:
    """Serialize bounded audio delivery for one active SmartPBX call."""

    def __init__(
        self,
        websocket: Any,
        context: CallContext,
        *,
        max_queue_frames: int = 8,
        on_frame_dropped: Callable[[], None] | None = None,
        on_transport_event: Callable[[str, int, str, int, int], None] | None = None,
    ) -> None:
        if isinstance(max_queue_frames, bool) or max_queue_frames < 1:
            raise ValueError("max_queue_frames must be positive")
        self._on_frame_dropped = on_frame_dropped
        self._on_transport_event = on_transport_event
        self._frames_dropped = 0
        self._websocket = websocket
        self._context = context
        self._queue: asyncio.Queue[_QueuedAudio] = asyncio.Queue(max_queue_frames)
        self._sender_task: asyncio.Task[None] | None = None
        self._generation = 0
        self._generation_turn_ids: dict[int, str] = {}
        self._first_sent_generations: set[int] = set()
        self._drained_generations: set[int] = set()
        self._residual_by_generation: dict[int, bytes] = {}
        self._cadence_by_generation: dict[int, _CadenceState] = {}
        self._queued_frames_by_generation: dict[int, int] = {}
        self._in_flight_generation: int | None = None
        self._underrun_tasks: dict[int, asyncio.Task[None]] = {}
        self._truncating_generation: int | None = None
        self._closed = False
        self._send_failed = asyncio.Event()

    @property
    def is_active(self) -> bool:
        return not self._closed and self._sender_task is not None and not self._sender_task.done()

    @property
    def frames_dropped_total(self) -> int:
        """Outbound frames discarded by backpressure, saturating."""
        return self._frames_dropped

    @property
    def send_failed(self) -> bool:
        """True once the outbound sender has died on a wire error."""
        return self._send_failed.is_set()

    async def wait_send_failed(self) -> None:
        """Block until the outbound sender dies, so the call can be ended."""
        await self._send_failed.wait()

    def start(self) -> None:
        """Start the one outbound sender once the WebSocket is ready."""
        if self._closed or self._sender_task is not None:
            return
        self._sender_task = asyncio.create_task(self._send_queued_audio())

    def bind_turn(self, generation: int, turn_id: str) -> None:
        """Associate a live media generation with an opaque pipeline turn id."""
        if generation == self._generation:
            if self._generation_turn_ids.get(generation) != turn_id:
                # Generations survive until a barge-in, whereas telemetry turns
                # do not.  A new opaque turn therefore starts fresh aggregate
                # cadence accounting without touching queued audio/fencing.
                self._cadence_by_generation[generation] = _CadenceState()
            self._generation_turn_ids[generation] = turn_id

    def set_transport_event_callback(
        self, callback: Callable[[str, int, str, int, int], None] | None
    ) -> None:
        self._on_transport_event = callback

    def cadence_summary_for_generation(self, generation: int) -> dict[str, int]:
        """Return fixed, bounded wire-cadence aggregates for one generation.

        This is intentionally a numeric-only snapshot.  It exposes neither
        Dialog identifiers nor audio/payload metadata, and it is a wire proxy
        rather than a playback acknowledgement.
        """
        state = self._cadence_by_generation.get(generation)
        if state is None:
            return {
                "frames_160b": 0,
                "frames_partial": 0,
                "frames_other": 0,
                "inter_send_gap_p95_ms": 0,
                "inter_send_gap_max_ms": 0,
                "queue_underruns": 0,
            }
        gaps = state.inter_send_gaps_ms
        if gaps:
            ordered = sorted(gaps)
            p95 = ordered[math.ceil(len(ordered) * 0.95) - 1]
            maximum = ordered[-1]
        else:
            p95 = maximum = 0
        return {
            "frames_160b": min(max(state.frames_160b, 0), _MAX_COUNTER),
            "frames_partial": min(max(state.frames_partial, 0), _MAX_COUNTER),
            "frames_other": min(max(state.frames_other, 0), _MAX_COUNTER),
            "inter_send_gap_p95_ms": min(max(p95, 0), _MAX_COUNTER),
            "inter_send_gap_max_ms": min(max(maximum, 0), _MAX_COUNTER),
            "queue_underruns": min(max(state.queue_underruns, 0), _MAX_COUNTER),
        }

    def _emit_transport_event(self, generation: int, stage: str) -> None:
        turn_id = self._generation_turn_ids.get(generation)
        callback = self._on_transport_event
        if turn_id is None or callback is None:
            return
        try:
            callback(turn_id, generation, stage, time.monotonic_ns(), self._frames_dropped)
        except Exception:
            # Telemetry must never interrupt the media delivery path.
            return

    async def send_audio(self, audio: bytes) -> None:
        """Reframe current μ-law audio, then apply existing queue backpressure.

        Arbitrary TTS reader chunks become ordered 160-byte/20 ms frames.  A
        final shorter residual belongs to the live generation until send_mark()
        establishes the existing delivery barrier.
        """
        if not self.is_active or not audio:
            return
        generation = self._generation
        if self._truncating_generation != generation:
            # A clear_audio() generation change starts a fresh utterance.
            self._truncating_generation = None
        state = self._cadence_by_generation.setdefault(generation, _CadenceState())
        state.utterance_open = True
        buffered = self._residual_by_generation.pop(generation, b"") + bytes(audio)
        if self._truncating_generation == generation:
            self._record_dropped_tail(generation, len(buffered))
            return
        complete_bytes = len(buffered) - (len(buffered) % _ULAW_FRAME_BYTES)
        residual = buffered[complete_bytes:]

        for offset in range(0, complete_bytes, _ULAW_FRAME_BYTES):
            if not await self._enqueue_frame(
                buffered[offset:offset + _ULAW_FRAME_BYTES], generation
            ):
                # A barge-in/close makes this stale and uncounted.  A sustained
                # overflow enters truncation; never retain a later residual that
                # could resume after a dropped frame and break the prefix.
                self._residual_by_generation.pop(generation, None)
                if self._truncating_generation == generation:
                    self._record_dropped_tail(
                        generation, len(buffered) - (offset + _ULAW_FRAME_BYTES)
                    )
                return

        if not self.is_active or generation != self._generation:
            return
        if self._truncating_generation == generation:
            return
        if residual:
            self._residual_by_generation[generation] = residual

    async def send_mark(self, _name: str) -> None:
        """Report speech completion once queued audio has reached the wire.

        Dialog defines no mark wire event. The caller treats completion as "she
        has stopped talking" and starts its re-prompt timer, so returning while
        paced audio is still queued would arm that timer mid-sentence. A dead
        sender, a barge-in, and close() all drain the queue, so this cannot
        outlive the call.
        """
        if not self.is_active:
            return
        generation = self._generation
        # The marker closes this generation before it flushes the final frame,
        # so a normal sentence end never becomes a sender underrun.
        self._close_generation_for_cadence(generation)
        residual = self._residual_by_generation.pop(generation, b"")
        if residual:
            await self._enqueue_frame(residual, generation)
        if not self.is_active or generation != self._generation:
            return
        await self._queue.join()
        if self.is_active and generation == self._generation and generation not in self._drained_generations:
            self._drained_generations.add(generation)
            self._emit_transport_event(generation, "queue_drained")
        if generation == self._generation:
            self._truncating_generation = None

    async def clear_audio(self) -> None:
        """Discard queued audio from the current generation without a wire event."""
        if not self.is_active:
            return
        old_generation = self._generation
        self._truncating_generation = None
        self._residual_by_generation.pop(old_generation, None)
        self._close_generation_for_cadence(old_generation)
        self._emit_transport_event(old_generation, "barge_clear")
        self._generation_turn_ids.pop(old_generation, None)
        self._first_sent_generations.discard(old_generation)
        self._drained_generations.discard(old_generation)
        self._generation += 1
        self._drain_queue()

    async def close(self) -> None:
        """Cancel delivery and make all later outbound operations no-ops."""
        if self._closed:
            return
        self._closed = True
        self._truncating_generation = None
        self._residual_by_generation.clear()
        self._close_all_generations_for_cadence()
        sender_task = self._sender_task
        if sender_task is not None:
            sender_task.cancel()
            # A sender that already died stores its exception; awaiting it here
            # would re-raise and skip the drain below.
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await sender_task
        self._drain_queue()

    async def _enqueue_frame(self, audio: bytes, generation: int) -> bool:
        """Apply the original bounded newest-frame policy to one wire frame."""
        if not audio or not self.is_active or generation != self._generation:
            return False
        if self._truncating_generation == generation:
            self._record_dropped_frame(generation)
            return False
        if self._queue.full():
            # Preserve the pre-existing short backpressure grace window.  A
            # clear/close race discards this stale frame without inflating drops.
            loop = asyncio.get_running_loop()
            deadline = loop.time() + _SEND_BACKPRESSURE_SECONDS
            while self._queue.full():
                if not self.is_active or self._generation != generation:
                    return False
                if loop.time() >= deadline:
                    break
                await asyncio.sleep(_SEND_BACKPRESSURE_POLL)
            if not self.is_active or self._generation != generation:
                return False
            if self._queue.full():
                self._truncating_generation = generation
                self._record_dropped_frame(generation)
                return False
        self._queue.put_nowait(_QueuedAudio(generation, audio))
        self._queued_frames_by_generation[generation] = (
            self._queued_frames_by_generation.get(generation, 0) + 1
        )
        return True

    async def _send_queued_audio(self) -> None:
        loop = asyncio.get_running_loop()
        next_send_at: float | None = None
        while True:
            queued = await self._queue.get()
            self._decrement_queued_frame(queued.generation)
            self._in_flight_generation = queued.generation
            try:
                if self._closed or queued.generation != self._generation:
                    # A dropped frame also disarms the cadence, so the first
                    # frame after a barge-in still goes out immediately.
                    next_send_at = None
                    continue
                now = loop.time()
                if next_send_at is None or next_send_at <= now:
                    next_send_at = now  # first frame of a reply, or behind
                else:
                    # Hold the queue at realtime so barge-in has audio left to
                    # cancel; cancelled on close(), which then drains.
                    await asyncio.sleep(next_send_at - now)
                    # This frame was already claimed from the queue, so a
                    # barge-in during the sleep could not drain it. Re-check, or
                    # one stale frame escapes after clear_audio().
                    if self._closed or queued.generation != self._generation:
                        next_send_at = None
                        continue
                payload = base64.b64encode(queued.audio).decode("ascii")
                try:
                    await self._websocket.send_text(json.dumps({
                        "event": "media",
                        "callId": self._context.call_id,
                        "accountId": self._context.account_id,
                        "media": {"payload": payload},
                    }))
                except asyncio.CancelledError:
                    raise
                except Exception:
                    # Never echo the wire error: it can carry payload bytes.
                    self._record_send_failure()
                    return
                self._record_wire_frame(queued.generation, len(queued.audio))
                if queued.generation == self._generation and queued.generation not in self._first_sent_generations:
                    self._first_sent_generations.add(queued.generation)
                    self._emit_transport_event(queued.generation, "first_media_sent")
                next_send_at += len(queued.audio) / _ULAW_BYTES_PER_SECOND
                self._schedule_underrun_check(queued.generation, next_send_at)
            finally:
                if self._in_flight_generation == queued.generation:
                    self._in_flight_generation = None
                self._queue.task_done()

    def _record_send_failure(self) -> None:
        """Signal a dead sender and release anything waiting on the queue."""
        self._send_failed.set()
        self._truncating_generation = None
        self._residual_by_generation.clear()
        self._close_all_generations_for_cadence()
        self._drain_queue()

    def _drain_queue(self) -> None:
        while not self._queue.empty():
            queued = self._queue.get_nowait()
            self._decrement_queued_frame(queued.generation)
            self._queue.task_done()

    def _record_dropped_frame(self, generation: int | None = None) -> None:
        self._frames_dropped = min(self._frames_dropped + 1, _MAX_COUNTER)
        self._emit_transport_event(self._generation if generation is None else generation, "frame_dropped")
        if self._on_frame_dropped is not None:
            self._on_frame_dropped()

    def _record_dropped_tail(self, generation: int, byte_count: int) -> None:
        """Count each unsent full or final partial wire frame in a dropped tail."""
        if (
            byte_count <= 0
            or not self.is_active
            or generation != self._generation
            or self._truncating_generation != generation
        ):
            return
        for _ in range(math.ceil(byte_count / _ULAW_FRAME_BYTES)):
            self._record_dropped_frame(generation)

    def _record_wire_frame(self, generation: int, size: int) -> None:
        state = self._cadence_by_generation.setdefault(generation, _CadenceState())
        if size == _ULAW_FRAME_BYTES:
            state.frames_160b = min(state.frames_160b + 1, _MAX_COUNTER)
        elif 0 < size < _ULAW_FRAME_BYTES:
            state.frames_partial = min(state.frames_partial + 1, _MAX_COUNTER)
        else:
            state.frames_other = min(state.frames_other + 1, _MAX_COUNTER)
        sent_ns = time.monotonic_ns()
        if state.previous_send_ns is not None and len(state.inter_send_gaps_ms) < _MAX_CADENCE_GAPS:
            gap_ms = min(max((sent_ns - state.previous_send_ns) // 1_000_000, 0), _MAX_COUNTER)
            state.inter_send_gaps_ms.append(gap_ms)
        state.previous_send_ns = sent_ns

    def _schedule_underrun_check(self, generation: int, deadline: float) -> None:
        self._cancel_underrun_check(generation)
        self._underrun_tasks[generation] = asyncio.create_task(
            self._record_underrun_if_open(generation, deadline)
        )

    async def _record_underrun_if_open(self, generation: int, deadline: float) -> None:
        try:
            delay = deadline - asyncio.get_running_loop().time()
            if delay > 0:
                await asyncio.sleep(delay)
            state = self._cadence_by_generation.get(generation)
            if (
                state is None
                or self._closed
                or generation != self._generation
                or not state.utterance_open
                or self._queued_frames_by_generation.get(generation, 0) > 0
                or self._in_flight_generation == generation
            ):
                return
            state.queue_underruns = min(state.queue_underruns + 1, _MAX_COUNTER)
            self._emit_transport_event(generation, "queue_underrun")
        except asyncio.CancelledError:
            return
        finally:
            current = self._underrun_tasks.get(generation)
            if current is asyncio.current_task():
                self._underrun_tasks.pop(generation, None)

    def _close_generation_for_cadence(self, generation: int) -> None:
        state = self._cadence_by_generation.get(generation)
        if state is not None:
            state.utterance_open = False
        self._cancel_underrun_check(generation)

    def _close_all_generations_for_cadence(self) -> None:
        for generation in list(self._cadence_by_generation):
            self._close_generation_for_cadence(generation)

    def _cancel_underrun_check(self, generation: int) -> None:
        task = self._underrun_tasks.pop(generation, None)
        if task is not None and task is not asyncio.current_task():
            task.cancel()

    def _decrement_queued_frame(self, generation: int) -> None:
        count = self._queued_frames_by_generation.get(generation, 0)
        if count <= 1:
            self._queued_frames_by_generation.pop(generation, None)
        else:
            self._queued_frames_by_generation[generation] = count - 1
