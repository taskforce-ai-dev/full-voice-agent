"""Runtime seam between the SmartPBX modules and the shared media pipeline.

These cover defects that the module-level suites cannot see because they stub
the shared MediaStreamSession/GoogleSTTStream out entirely.
"""

from __future__ import annotations

import asyncio
import threading
import time

import pytest

from smartpbx_protocol import CallContext, MediaFormat
from smartpbx_session import KavyaSmartPBXSession


CONTEXT = CallContext(
    "media", "safe", "0771234567", "0770000000", "account", MediaFormat("g711_ulaw", 8000)
)


class Transport:
    async def clear_audio(self) -> None:
        pass


class Pipeline:
    def __init__(self, stt=None) -> None:
        self.transfer_pending = False
        self._stt = stt
        self._endpointing_handle = None
        self.full_transcript = []

    def _cancel_reprompt(self) -> None:
        pass

    def _write_audio_dump(self) -> None:
        pass


class BlockingSTT:
    """Stand-in for GoogleSTTStream.stop(), which joins a thread for up to 5s."""

    def __init__(self, block_seconds: float) -> None:
        self._block_seconds = block_seconds
        self.stop_thread: int | None = None

    def stop(self) -> None:
        self.stop_thread = threading.get_ident()
        time.sleep(self._block_seconds)


def _session(pipeline) -> KavyaSmartPBXSession:
    async def post(**_):
        raise AssertionError("empty transcript must not post")

    return KavyaSmartPBXSession(
        CONTEXT, Transport(), pipeline=pipeline, post_call_processor=post,
        welcome_text="", llm_provider="openai", model="m",
    )


@pytest.mark.asyncio
async def test_finish_stops_stt_off_the_event_loop_thread():
    stt = BlockingSTT(0.01)
    session = _session(Pipeline(stt))

    await session.finish(False)

    assert stt.stop_thread is not None, "stop() must still be called"
    assert stt.stop_thread != threading.get_ident(), (
        "STT stop() joins a worker thread for up to 5s; running it on the event "
        "loop freezes audio and timers for every other concurrent call"
    )


@pytest.mark.asyncio
async def test_finish_leaves_the_event_loop_responsive_for_other_calls():
    ticks = 0

    async def ticker() -> None:
        nonlocal ticks
        while True:
            await asyncio.sleep(0.01)
            ticks += 1

    session = _session(Pipeline(BlockingSTT(0.2)))
    ticker_task = asyncio.create_task(ticker())
    await asyncio.sleep(0)

    await session.finish(False)
    ticker_task.cancel()
    await asyncio.gather(ticker_task, return_exceptions=True)

    assert ticks >= 5, (
        f"event loop advanced only {ticks} ticks while one call hung up; "
        "other calls' audio and timers must keep running"
    )
