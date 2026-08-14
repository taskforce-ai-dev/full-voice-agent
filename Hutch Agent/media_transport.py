from __future__ import annotations

import asyncio
import base64
import json
from typing import Any, Protocol


class MediaTransport(Protocol):
    async def send_audio(self, audio: bytes) -> None:
        ...

    async def send_mark(self, name: str) -> None:
        ...

    async def clear_audio(self) -> None:
        ...


class TwilioMediaTransport:
    def __init__(self, websocket: Any, stream_sid_getter):
        self.websocket = websocket
        self.stream_sid_getter = stream_sid_getter
        self._lock = asyncio.Lock()

    async def send_audio(self, audio: bytes) -> None:
        payload = base64.b64encode(audio).decode("ascii")
        async with self._lock:
            await self.websocket.send_text(json.dumps({
                "event": "media",
                "streamSid": self.stream_sid_getter(),
                "media": {"payload": payload},
            }))

    async def send_mark(self, name: str) -> None:
        async with self._lock:
            await self.websocket.send_text(json.dumps({
                "event": "mark",
                "streamSid": self.stream_sid_getter(),
                "mark": {"name": name},
            }))

    async def clear_audio(self) -> None:
        async with self._lock:
            await self.websocket.send_text(json.dumps({
                "event": "clear",
                "streamSid": self.stream_sid_getter(),
            }))
