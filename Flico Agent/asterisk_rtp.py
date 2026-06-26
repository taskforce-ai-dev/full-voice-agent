from __future__ import annotations

import asyncio
import logging
import random
import socket
import struct
from collections.abc import Awaitable, Callable

logger = logging.getLogger(__name__)

RTP_HEADER_SIZE = 12
PCMU_PAYLOAD_TYPE = 0
PCMU_FRAME_BYTES = 160
PCMU_CLOCK_RATE = 8000


class RtpPortAllocator:
    def __init__(self, host: str, start: int, end: int):
        self.host = host
        self.start = start
        self.end = end
        self._in_use: set[int] = set()

    def allocate(self) -> int:
        for port in range(self.start, self.end + 1):
            if port in self._in_use:
                continue
            self._in_use.add(port)
            return port
        raise RuntimeError("No RTP ports available")

    def release(self, port: int) -> None:
        self._in_use.discard(port)

    def snapshot(self) -> dict:
        total = self.end - self.start + 1
        in_use = sorted(self._in_use)
        return {
            "host": self.host,
            "start": self.start,
            "end": self.end,
            "total": total,
            "used": len(in_use),
            "free": max(total - len(in_use), 0),
            "in_use": in_use,
        }


class AsteriskRtpTransport(asyncio.DatagramProtocol):
    def __init__(
        self,
        bind_host: str,
        bind_port: int,
        on_audio: Callable[[bytes], Awaitable[None]],
    ):
        self.bind_host = bind_host
        self.bind_port = bind_port
        self.on_audio = on_audio
        self.transport: asyncio.DatagramTransport | None = None
        self.remote: tuple[str, int] | None = None
        self._remote_event = asyncio.Event()
        self.sequence = random.randint(0, 65535)
        self.timestamp = random.randint(0, 2**32 - 1)
        self.ssrc = random.randint(1, 2**32 - 1)
        self._closed = asyncio.Event()
        self._send_lock = asyncio.Lock()

    async def start(self) -> None:
        loop = asyncio.get_running_loop()
        transport, _ = await loop.create_datagram_endpoint(
            lambda: self,
            local_addr=(self.bind_host, self.bind_port),
            family=socket.AF_INET,
        )
        self.transport = transport
        logger.info("Asterisk RTP listener bound on %s:%d", self.bind_host, self.bind_port)

    @property
    def is_active(self) -> bool:
        return not self._closed.is_set()

    async def stop(self) -> None:
        self._closed.set()
        self._remote_event.set()
        transport = self.transport
        self.transport = None
        if transport and not transport.is_closing():
            transport.close()

    def connection_lost(self, exc):
        self.transport = None
        self._closed.set()
        self._remote_event.set()

    def error_received(self, exc):
        logger.debug("Asterisk RTP transport error; closing sender: %s", exc)
        self._closed.set()
        self._remote_event.set()
        transport = self.transport
        self.transport = None
        if transport and not transport.is_closing():
            transport.close()

    def set_remote(self, host: str, port: int) -> None:
        if self._closed.is_set():
            return
        self.remote = (host, int(port))
        self._remote_event.set()
        logger.info("Asterisk RTP remote set to %s:%s", host, port)

    def datagram_received(self, data: bytes, addr):
        if self._closed.is_set():
            return
        if len(data) < RTP_HEADER_SIZE:
            return
        if self.remote is None:
            self.set_remote(addr[0], addr[1])
        header = data[:RTP_HEADER_SIZE]
        first, payload_type, _seq, _timestamp, _ssrc = struct.unpack("!BBHII", header)
        version = first >> 6
        if version != 2 or (payload_type & 0x7F) != PCMU_PAYLOAD_TYPE:
            return
        payload = data[RTP_HEADER_SIZE:]
        asyncio.create_task(self.on_audio(payload))

    async def send_audio(self, audio: bytes) -> None:
        if self._closed.is_set() or not audio:
            return
        if not self.remote:
            try:
                await asyncio.wait_for(self._remote_event.wait(), timeout=2.0)
            except asyncio.TimeoutError:
                logger.warning("Dropping RTP audio; remote address is not known yet")
                return
        if self._closed.is_set() or not self.remote:
            return
        async with self._send_lock:
            for offset in range(0, len(audio), PCMU_FRAME_BYTES):
                if self._closed.is_set():
                    return
                transport = self.transport
                if not transport or transport.is_closing():
                    return
                frame = audio[offset:offset + PCMU_FRAME_BYTES]
                if not frame:
                    continue
                header = struct.pack(
                    "!BBHII",
                    0x80,
                    PCMU_PAYLOAD_TYPE,
                    self.sequence,
                    self.timestamp,
                    self.ssrc,
                )
                try:
                    transport.sendto(header + frame, self.remote)
                except (OSError, RuntimeError):
                    logger.debug("Dropping RTP audio after transport close", exc_info=True)
                    await self.stop()
                    return
                self.sequence = (self.sequence + 1) & 0xFFFF
                self.timestamp = (self.timestamp + len(frame)) & 0xFFFFFFFF
                await asyncio.sleep(len(frame) / PCMU_CLOCK_RATE)

    async def send_mark(self, name: str) -> None:
        # RTP has no Twilio-style mark event. The session clears speaking state
        # after each send_audio stream returns.
        return

    async def clear_audio(self) -> None:
        # There is no remote playback buffer to clear in the MVP. Barge-in stops
        # future local TTS packets from being generated.
        return
