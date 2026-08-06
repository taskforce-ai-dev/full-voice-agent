"""Bounded outbound media transport for Dialog SmartPBX calls."""

from __future__ import annotations

import asyncio
import base64
import contextlib
import json
from dataclasses import dataclass
from typing import Any

from smartpbx_protocol import CallContext


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
        self._speaking_acknowledged = False

    @property
    def is_active(self) -> bool:
        return not self._closed and self._sender_task is not None and not self._sender_task.done()

    @property
    def is_speaking_acknowledged(self) -> bool:
        return self._speaking_acknowledged

    def start(self) -> None:
        """Start the one outbound sender once the WebSocket is ready."""
        if self._closed or self._sender_task is not None:
            return
        self._sender_task = asyncio.create_task(self._send_queued_audio())

    async def send_audio(self, audio: bytes) -> None:
        """Queue audio without allowing a slow network peer to block callers."""
        if not self.is_active:
            return
        self._speaking_acknowledged = True
        if self._queue.full():
            self._queue.get_nowait()
            self._queue.task_done()
        self._queue.put_nowait(_QueuedAudio(self._generation, bytes(audio)))

    async def send_mark(self, _name: str) -> None:
        """Acknowledge local speech completion; Dialog defines no mark wire event."""
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
        while True:
            queued = await self._queue.get()
            try:
                if not self._closed and queued.generation == self._generation:
                    payload = base64.b64encode(queued.audio).decode("ascii")
                    await self._websocket.send_text(json.dumps({
                        "event": "media",
                        "callId": self._context.call_id,
                        "accountId": self._context.account_id,
                        "media": {"payload": payload},
                    }))
            finally:
                self._queue.task_done()

    def _drain_queue(self) -> None:
        while not self._queue.empty():
            self._queue.get_nowait()
            self._queue.task_done()
