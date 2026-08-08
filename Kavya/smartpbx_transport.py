"""Bounded outbound media transport for Dialog SmartPBX calls."""

from __future__ import annotations

import asyncio
import base64
import contextlib
import json
from dataclasses import dataclass
from typing import Any

from smartpbx_protocol import CallContext


# g711_ulaw at 8 kHz carries one byte per sample, so a frame's wall-clock
# duration is simply its length over this rate.
_ULAW_BYTES_PER_SECOND = 8000


@dataclass(frozen=True)
class _QueuedAudio:
    generation: int
    audio: bytes


class SmartPBXMediaTransport:
    """Serialize bounded audio delivery for one active SmartPBX call."""

    def __init__(self, websocket: Any, context: CallContext, *, max_queue_frames: int) -> None:
        if isinstance(max_queue_frames, bool) or max_queue_frames < 1:
            raise ValueError("max_queue_frames must be positive")
        self._websocket = websocket
        self._context = context
        self._queue: asyncio.Queue[_QueuedAudio] = asyncio.Queue(max_queue_frames)
        self._sender_task: asyncio.Task[None] | None = None
        self._generation = 0
        self._closed = False
        self._send_failed = asyncio.Event()

    @property
    def is_active(self) -> bool:
        return not self._closed and self._sender_task is not None and not self._sender_task.done()

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

    async def send_audio(self, audio: bytes) -> None:
        """Queue audio without allowing a slow network peer to block callers."""
        if not self.is_active:
            return
        if self._queue.full():
            self._queue.get_nowait()
            self._queue.task_done()
        self._queue.put_nowait(_QueuedAudio(self._generation, bytes(audio)))

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
        await self._queue.join()

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
        sender_task = self._sender_task
        if sender_task is not None:
            sender_task.cancel()
            # A sender that already died stores its exception; awaiting it here
            # would re-raise and skip the drain below.
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await sender_task
        self._drain_queue()

    async def _send_queued_audio(self) -> None:
        loop = asyncio.get_running_loop()
        next_send_at: float | None = None
        while True:
            queued = await self._queue.get()
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
                next_send_at += len(queued.audio) / _ULAW_BYTES_PER_SECOND
            finally:
                self._queue.task_done()

    def _record_send_failure(self) -> None:
        """Signal a dead sender and release anything waiting on the queue."""
        self._send_failed.set()
        self._drain_queue()

    def _drain_queue(self) -> None:
        while not self._queue.empty():
            self._queue.get_nowait()
            self._queue.task_done()
