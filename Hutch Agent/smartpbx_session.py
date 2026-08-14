"""smartpbx_session.py -- Dialog SmartPBX lifecycle adapter for Selina.

Binds one Dialog SmartPBX call into `server.MediaStreamSession`, which was
originally built around Twilio's Media Streams wire format. SmartPBX drives
that same pipeline (STT -> KB retrieval -> LLM tool-use loop -> TTS) directly
through method calls instead of a Twilio-shaped WebSocket event loop.

Modeled on `Kavya/smartpbx_session.py`, trimmed down: Hutch has no PMS/
booking tools, no live call transfer, and no post-call dashboard pipeline, so
none of that plumbing is reproduced here. The only tool available is
`notify_human_handover` (see tools.py / handover.py).
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from smartpbx_protocol import CallContext
from smartpbx_transport import SmartPBXMediaTransport

logger = logging.getLogger(__name__)


class HutchSmartPBXSession:
    """Own one Dialog call while reusing Selina's existing MediaStreamSession."""

    def __init__(
        self,
        context: CallContext,
        transport: SmartPBXMediaTransport,
        *,
        pipeline: Any | None = None,
    ) -> None:
        self._context = context
        self._transport = transport
        self._pipeline = pipeline

        loop = asyncio.get_running_loop()
        self._terminal_future: asyncio.Future[dict[str, Any]] = loop.create_future()
        self._start_lock = asyncio.Lock()
        self._finish_lock = asyncio.Lock()
        self._start_task: asyncio.Task[None] | None = None
        self._finish_task: asyncio.Task[None] | None = None
        self._welcome_task: asyncio.Task[None] | None = None

    @property
    def terminal_future(self) -> asyncio.Future[dict[str, Any]]:
        return self._terminal_future

    async def start(self) -> None:
        async with self._start_lock:
            if self._start_task is None:
                self._start_task = asyncio.create_task(self._start_once())
            task = self._start_task
        await asyncio.shield(task)

    async def feed_audio(self, payload: bytes) -> None:
        if self._finish_task is not None:
            return
        if self._start_task is None or not self._start_task.done():
            raise RuntimeError("SmartPBX session is not started")
        pipeline = self._require_pipeline()
        if getattr(pipeline, "_stt", None) is not None:
            pipeline._stt.feed(bytes(payload))

    async def finish(self, schedule_post_call: bool = False) -> None:
        # schedule_post_call is part of the SmartPBXGateway._GatewaySession
        # contract; Hutch has no post-call pipeline, so it is accepted and
        # ignored.
        async with self._finish_lock:
            if self._finish_task is None:
                self._finish_task = asyncio.create_task(self._finish_once())
            task = self._finish_task
        await asyncio.shield(task)

    async def _start_once(self) -> None:
        import server

        pipeline = self._require_pipeline(create=True)
        pipeline.lang = "en"
        pipeline.call_sid = self._context.other_leg_call_id
        pipeline._event_loop = asyncio.get_running_loop()
        pipeline._set_handover_context()

        pipeline._stt = server._make_stt(
            on_final_result=pipeline._on_stt_result,
            on_interim_result=pipeline._on_stt_interim,
            lang="en",
        )
        pipeline._stt.start()

        welcome_text = server.MEDIA_STREAM_WELCOME["en"]
        self._welcome_task = asyncio.create_task(pipeline._speak(welcome_text))

    async def _finish_once(self) -> None:
        try:
            pipeline = self._pipeline
            if pipeline is None:
                return
            endpointing_handle = getattr(pipeline, "_endpointing_handle", None)
            if endpointing_handle is not None:
                endpointing_handle.cancel()
                pipeline._endpointing_handle = None
            stt = getattr(pipeline, "_stt", None)
            if stt is not None:
                # stop() may join a worker thread for up to a few seconds.
                # This loop is shared by every concurrent call, so it must
                # not block here.
                await asyncio.to_thread(stt.stop)
            if self._welcome_task is not None:
                if not self._welcome_task.done():
                    self._welcome_task.cancel()
                await asyncio.gather(self._welcome_task, return_exceptions=True)
        finally:
            if not self._terminal_future.done():
                self._terminal_future.set_result({"transferred": False})

    def _require_pipeline(self, create: bool = False) -> Any:
        if self._pipeline is None:
            if not create:
                raise RuntimeError("Selina media pipeline is unavailable")
            import server

            anthropic_client = openai_client = gemini_client = None
            if server.LLM_PROVIDER == "claude":
                anthropic_client = server._get_anthropic_client()
            elif server.LLM_PROVIDER == "gemini":
                gemini_client = server._get_gemini_client()
            else:
                openai_client = server._get_client()
            self._pipeline = server.MediaStreamSession(
                websocket=None,
                lang="en",
                anthropic_client=anthropic_client,
                openai_client=openai_client,
                gemini_client=gemini_client,
            )
            # MediaStreamSession normally sends raw Twilio Media Streams JSON
            # frames over `self.ws`. SmartPBXMediaTransport exposes the
            # narrower send_audio/send_mark/clear_audio surface instead, so
            # swap in a thin adapter that speaks MediaStreamSession's wire
            # calls against the transport.
            self._pipeline.ws = _TransportWebSocketAdapter(self._transport)
        return self._pipeline


class _TransportWebSocketAdapter:
    """Translate MediaStreamSession's `ws.send_text(json...)` calls into
    SmartPBXMediaTransport's send_audio/send_mark/clear_audio methods.

    MediaStreamSession (built for Twilio Media Streams) always writes JSON
    frames shaped `{"event": "media"|"mark"|"clear", ...}` to `self.ws`. This
    adapter is the smallest translation layer that lets that code run
    unmodified against the SmartPBX transport's typed methods.
    """

    def __init__(self, transport: SmartPBXMediaTransport) -> None:
        self._transport = transport

    async def send_text(self, raw: str) -> None:
        import base64
        import json

        message = json.loads(raw)
        event = message.get("event")
        if event == "media":
            payload = message.get("media", {}).get("payload", "")
            await self._transport.send_audio(base64.b64decode(payload))
        elif event == "mark":
            name = message.get("mark", {}).get("name", "")
            await self._transport.send_mark(name)
        elif event == "clear":
            await self._transport.clear_audio()
        else:
            logger.debug("Unhandled MediaStreamSession wire event: %s", event)
