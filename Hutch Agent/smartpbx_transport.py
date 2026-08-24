"""Bounded outbound media transport for Dialog SmartPBX calls."""

from __future__ import annotations

import asyncio
import base64
import contextlib
import json
from dataclasses import dataclass
from typing import Any

from media_transport import MediaTransport
from smartpbx_protocol import CallContext


# g711_ulaw at 8 kHz carries one byte per sample, so a frame's wall-clock
# duration is simply its length over this rate.
_ULAW_BYTES_PER_SECOND = 8000
# Grace window a full outbound queue waits for the realtime sender to drain
# before it gives up and drops the newest frame. Bursty TTS (ElevenLabs can
# generate audio well ahead of its own playback duration) outruns realtime
# pacing, so a short wait turns most would-be overflow drops into
# backpressure while the hard drop-newest remains the backstop for sustained
# overload.
_SEND_BACKPRESSURE_SECONDS = 0.2
_SEND_BACKPRESSURE_POLL = 0.005


@dataclass(frozen=True)
class _QueuedAudio:
    generation: int
    audio: bytes


class SmartPBXMediaTransport(MediaTransport):
    """Serialize bounded audio delivery for one active SmartPBX call."""

    def __init__(
        self, websocket: Any, context: CallContext, *, max_queue_frames: int
    ) -> None:
        if isinstance(max_queue_frames, bool) or max_queue_frames < 1:
            raise ValueError("max_queue_frames must be positive")
        self._websocket = websocket
        self._context = context
        self._queue: asyncio.Queue[_QueuedAudio] = asyncio.Queue(max_queue_frames)
        self._sender_task: asyncio.Task[None] | None = None
        self._generation = 0
        self._closed = False
        self._speaking_acknowledged = False

    @property
    def is_active(self) -> bool:
        return not self._closed and self._sender_task is not None and not self._sender_task.done()

    @property
    def is_speaking_acknowledged(self) -> bool:
        return self._speaking_acknowledged

    def start(self) -> None:
        """Start the single outbound sender once the WebSocket is ready."""
        if self._closed or self._sender_task is not None:
            return
        self._sender_task = asyncio.create_task(self._send_queued_audio())

    async def send_audio(self, audio: bytes) -> None:
        """Queue audio without allowing a slow network peer to block callers.

        On overflow this drops the NEWEST frame (the tail of the reply not
        yet queued), not the oldest. Dialog SmartPBX has no wire-level
        "stop talking" event, so any audio already sent is already sitting in
        Dialog's own playback buffer and cannot be recalled -- the only lever
        this transport has is never sending it in the first place. Dropping
        the oldest queued (not-yet-sent) frame instead would cut a hole out
        of the *middle* of already-committed speech, which is worse.
        """
        if not self.is_active:
            return
        self._speaking_acknowledged = True
        generation = self._generation
        if self._queue.full():
            loop = asyncio.get_running_loop()
            deadline = loop.time() + _SEND_BACKPRESSURE_SECONDS
            while self._queue.full():
                if not self.is_active or self._generation != generation:
                    return
                if loop.time() >= deadline:
                    break
                await asyncio.sleep(_SEND_BACKPRESSURE_POLL)
            if not self.is_active or self._generation != generation:
                return
            if self._queue.full():
                # Backstop: still full after the grace window -- drop this
                # newest frame rather than evict an earlier, already-queued
                # one.
                return
        self._queue.put_nowait(_QueuedAudio(generation, audio))

    async def send_mark(self, name: str) -> None:
        """Acknowledge local speech completion; SmartPBX has no mark wire event."""
        if self.is_active:
            self._speaking_acknowledged = False

    async def clear_audio(self) -> None:
        """Discard queued audio from the current generation without a wire event."""
        if not self.is_active:
            return
        self._generation += 1
        self._drain_queue()

    async def close(self) -> None:
        """Cancel delivery and make all later outbound operations no-ops."""
        if self._closed:
            return
        self._closed = True
        self._speaking_acknowledged = False
        sender_task = self._sender_task
        if sender_task is not None:
            sender_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await sender_task
        self._drain_queue()

    async def _send_queued_audio(self) -> None:
        """Send queued frames paced at realtime.

        Without pacing, TTS audio for a whole reply reaches the WebSocket
        (and Dialog's own playback buffer) within a second or two of
        generation -- far faster than its actual speaking duration. By the
        time a barge-in is detected and ``clear_audio()`` bumps the
        generation, there is nothing left in THIS queue to discard, because
        it was already sent. Holding each frame back until its predecessor
        has finished "playing" (by wall clock) keeps unsent audio in the
        queue for a barge-in to actually cancel, which is the only place
        this transport can still act -- Dialog defines no wire-level "clear"
        event of its own.
        """
        loop = asyncio.get_running_loop()
        next_send_at: float | None = None
        while True:
            queued = await self._queue.get()
            try:
                if self._closed or queued.generation != self._generation:
                    # A dropped/stale frame also disarms the cadence, so the
                    # first frame of the next reply (or after a barge-in)
                    # goes out immediately rather than waiting on a deadline
                    # computed for audio that never played.
                    next_send_at = None
                    continue
                now = loop.time()
                if next_send_at is None or next_send_at <= now:
                    next_send_at = now
                else:
                    await asyncio.sleep(next_send_at - now)
                    # This frame was already claimed from the queue before
                    # the sleep, so a barge-in during the sleep could not
                    # drain it out from under us -- re-check here or one
                    # stale frame escapes after clear_audio().
                    if self._closed or queued.generation != self._generation:
                        next_send_at = None
                        continue
                payload = base64.b64encode(queued.audio).decode("ascii")
                await self._websocket.send_text(json.dumps({
                    "event": "media",
                    "callId": self._context.call_id,
                    "accountId": self._context.account_id,
                    "media": {"payload": payload},
                }))
                next_send_at += len(queued.audio) / _ULAW_BYTES_PER_SECOND
            finally:
                self._queue.task_done()

    def _drain_queue(self) -> None:
        while not self._queue.empty():
            self._queue.get_nowait()
            self._queue.task_done()
