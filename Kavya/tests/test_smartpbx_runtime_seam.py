"""Runtime seam between the SmartPBX modules and the shared media pipeline.

These cover defects that the module-level suites cannot see because they stub
the shared MediaStreamSession/GoogleSTTStream out entirely.
"""

from __future__ import annotations

import asyncio
import json
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


class FailingSocket:
    """A peer whose send_text starts raising after `ok_sends` frames."""

    def __init__(self, ok_sends: int = 0) -> None:
        self.ok_sends = ok_sends
        self.sent = 0
        self.headers = {"X-Kavya-SmartPBX-Token": "token"}
        self.accepted = False
        self.close_calls: list[tuple[int, str]] = []
        self.messages: asyncio.Queue[str] = asyncio.Queue()

    async def accept(self) -> None:
        self.accepted = True

    async def receive_text(self) -> str:
        return await self.messages.get()

    async def close(self, code: int = 1000, reason: str = "") -> None:
        self.close_calls.append((code, reason))

    async def send_text(self, _message: str) -> None:
        if self.sent >= self.ok_sends:
            raise RuntimeError("SENTINEL_WIRE_ERROR")
        self.sent += 1


def _transport(socket, frames: int = 8):
    from smartpbx_transport import SmartPBXMediaTransport

    return SmartPBXMediaTransport(socket, CONTEXT, max_queue_frames=frames)


@pytest.mark.asyncio
async def test_transport_records_a_dead_sender_instead_of_going_silent():
    socket = FailingSocket()
    transport = _transport(socket)
    transport.start()

    await transport.send_audio(b"\xff" * 160)
    await asyncio.wait_for(transport.wait_send_failed(), timeout=1)

    assert transport.send_failed is True, (
        "a send_text exception kills the sender task; is_active then makes every "
        "later send_audio a silent no-op with nothing signalled to the session"
    )
    assert transport.is_active is False


@pytest.mark.asyncio
async def test_transport_close_survives_a_dead_sender_and_drains():
    socket = FailingSocket()
    transport = _transport(socket)
    transport.start()
    await transport.send_audio(b"\xff" * 160)
    await asyncio.wait_for(transport.wait_send_failed(), timeout=1)
    await transport.send_audio(b"\xff" * 160)

    await transport.close()  # must not re-raise the sender's stored exception

    assert transport._queue.empty(), "close() must still drain the queue"


@pytest.mark.asyncio
async def test_gateway_ends_the_call_when_the_outbound_sender_dies():
    from dataclasses import replace

    from smartpbx_gateway import (
        SmartPBXGateway, SmartPBXSessionRegistry, SmartPBXSettings,
    )

    settings = replace(
        SmartPBXSettings.from_env({
            "ENABLE_SMARTPBX_WSS": "true", "SMARTPBX_WS_TOKEN": "token",
            "SMARTPBX_ACCOUNT_ID": "account-1",
        }),
        idle_timeout_seconds=90,
    )
    socket = FailingSocket()
    socket.messages.put_nowait(json.dumps({"event": "start", "start": {
        "callId": "call-1", "otherLegCallId": "other-1",
        "callerIdNumber": "caller", "calleeIdNumber": "callee",
        "accountId": "account-1",
        "mediaFormat": {"encoding": "g711_ulaw", "sampleRate": 8000},
    }}))

    class Session:
        def __init__(self, transport) -> None:
            self._transport = transport
            self.transfer_pending = False
            self.terminal_future = asyncio.get_running_loop().create_future()
            self.finishes = 0

        async def start(self) -> None:
            await self._transport.send_audio(b"\xff" * 160)

        async def feed_audio(self, _audio) -> None:
            pass

        async def finish(self, schedule_post_call: bool = False) -> None:
            self.finishes += int(schedule_post_call)

    async def factory(_context, transport, _sink=None):
        return Session(transport)

    gateway = SmartPBXGateway(settings, SmartPBXSessionRegistry(4))
    # Well under the 90s idle timeout: the call must end on the failure signal,
    # not by leaving the guest in dead air until the socket times out.
    await asyncio.wait_for(gateway.handle(socket, factory), timeout=5)

    assert socket.close_calls, "the gateway must close the dead call"
    assert gateway.snapshot()["active_sessions"] == 0


def _failing_stt_loop(monkeypatch, failures: int = 10_000):
    """Drive GoogleSTTStream._loop with a stream that always fails immediately."""
    import server

    stream = server.GoogleSTTStream(on_final_result=lambda *_: None, lang="en")
    stream._running = True
    attempts = 0

    def boom():
        nonlocal attempts
        attempts += 1
        # Escape hatch so an unbounded loop still terminates the test run.
        if attempts >= failures:
            stream._running = False
        raise RuntimeError("DefaultCredentialsError: credentials file rotated")

    sleeps: list[float] = []
    monkeypatch.setattr(stream, "_run_one_stream", boom)
    monkeypatch.setattr(server.time, "sleep", lambda seconds: sleeps.append(seconds))
    stream._loop()
    return stream, (lambda: attempts), sleeps


def test_stt_restart_loop_backs_off_instead_of_spinning(monkeypatch):
    _stream, attempts, sleeps = _failing_stt_loop(monkeypatch)

    assert attempts() <= 12, (
        f"{attempts()} restart attempts with no delay: a synchronous "
        "SpeechClient() failure spins the thread and floods the log ring"
    )
    assert sleeps, "a failed stream restart must back off before retrying"
    assert sleeps == sorted(sleeps), "backoff must be non-decreasing"
    assert max(sleeps) <= 5.0, "backoff must stay capped near 5s"


def test_stt_restart_loop_gives_up_so_the_call_can_end(monkeypatch):
    stream, attempts, _sleeps = _failing_stt_loop(monkeypatch, failures=10_000)

    assert attempts() < 10_000, "the loop stopped only because the test forced it to"
    assert stream._running is False, (
        "after a capped run of consecutive failures the STT thread must stop "
        "so the call ends cleanly rather than churning gRPC channels"
    )


def test_stt_restart_loop_resets_backoff_after_a_healthy_stream(monkeypatch):
    import server

    stream = server.GoogleSTTStream(on_final_result=lambda *_: None, lang="en")
    stream._running = True
    calls = 0

    def flaky():
        nonlocal calls
        calls += 1
        if calls in (1, 2, 4, 5):
            raise RuntimeError("transient")
        if calls == 3:
            return  # a healthy stream ended normally
        stream._running = False
        raise RuntimeError("transient")

    sleeps: list[float] = []
    monkeypatch.setattr(stream, "_run_one_stream", flaky)
    monkeypatch.setattr(server.time, "sleep", lambda seconds: sleeps.append(seconds))
    stream._loop()

    assert len(sleeps) >= 4
    assert sleeps[2] <= sleeps[1], (
        "a stream that ran successfully must reset the backoff, otherwise a "
        "long healthy call inherits the delay from an unrelated early blip"
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
