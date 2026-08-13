"""Runtime seam between the SmartPBX modules and the shared media pipeline.

These cover defects that the module-level suites cannot see because they stub
the shared MediaStreamSession/GoogleSTTStream out entirely.
"""

from __future__ import annotations

import asyncio
import json
import threading
import types
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

    def _on_stt_result(self, _text) -> None:
        pass

    def _on_stt_interim(self, _text) -> None:
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


class RecordingSocket:
    """Records the arrival time of every outbound frame."""

    def __init__(self) -> None:
        self.arrivals: list[float] = []

    async def send_text(self, _message: str) -> None:
        self.arrivals.append(asyncio.get_running_loop().time())


FRAME = b"\xff" * 160  # 160 bytes of g711_ulaw @ 8 kHz == 20ms of audio
FRAME_SECONDS = 0.02


@pytest.mark.asyncio
async def test_outbound_audio_is_paced_at_realtime():
    socket = RecordingSocket()
    transport = _transport(socket, frames=64)
    transport.start()

    started = asyncio.get_running_loop().time()
    for _ in range(10):
        await transport.send_audio(FRAME)
    await asyncio.wait_for(transport.send_mark("tts_done"), timeout=5)
    elapsed = asyncio.get_running_loop().time() - started
    await transport.close()

    assert len(socket.arrivals) == 10
    # 10 frames of 20ms is 200ms of speech. Sending it all in ~1ms is what makes
    # barge-in useless: clear_audio() then has an empty queue to cancel.
    assert elapsed >= 8 * FRAME_SECONDS, (
        f"200ms of audio was fully on the wire in {elapsed * 1000:.0f}ms"
    )


@pytest.mark.asyncio
async def test_pacing_does_not_delay_the_first_frame():
    socket = RecordingSocket()
    transport = _transport(socket)
    transport.start()

    started = asyncio.get_running_loop().time()
    await transport.send_audio(FRAME)
    for _ in range(20):
        if socket.arrivals:
            break
        await asyncio.sleep(0.001)
    await transport.close()

    assert socket.arrivals, "the first frame must be sent"
    assert socket.arrivals[0] - started < 0.01, (
        "pacing must not add latency before the first frame of a reply"
    )


@pytest.mark.asyncio
async def test_a_new_utterance_after_silence_still_starts_immediately():
    socket = RecordingSocket()
    transport = _transport(socket)
    transport.start()
    await transport.send_audio(FRAME)
    await asyncio.wait_for(transport.send_mark("tts_done"), timeout=5)
    await asyncio.sleep(0.05)  # caller thinking; cadence must not stay armed

    started = asyncio.get_running_loop().time()
    await transport.send_audio(FRAME)
    await asyncio.wait_for(transport.send_mark("tts_done"), timeout=5)
    first_of_second_utterance = socket.arrivals[1] - started
    await transport.close()

    assert first_of_second_utterance < 0.01


@pytest.mark.asyncio
async def test_barge_in_cancels_audio_that_has_not_been_sent():
    socket = RecordingSocket()
    transport = _transport(socket, frames=64)
    transport.start()

    for _ in range(30):  # 600ms of speech
        await transport.send_audio(FRAME)
    await asyncio.sleep(0.05)
    await transport.clear_audio()
    sent_at_barge_in = len(socket.arrivals)
    await asyncio.sleep(0.05)
    await transport.close()

    assert sent_at_barge_in <= 6, (
        f"{sent_at_barge_in}/30 frames were already on the wire at barge-in, so "
        "clear_audio() had nothing left to cancel"
    )
    assert len(socket.arrivals) == sent_at_barge_in, (
        "no further frames may be sent after clear_audio()"
    )


@pytest.mark.asyncio
async def test_send_mark_waits_for_queued_audio_to_reach_the_wire():
    socket = RecordingSocket()
    transport = _transport(socket, frames=64)
    transport.start()
    for _ in range(10):
        await transport.send_audio(FRAME)

    await asyncio.wait_for(transport.send_mark("tts_done"), timeout=5)

    # The caller starts its re-prompt timer when the mark returns, so the mark
    # must not fire while paced audio is still queued.
    assert len(socket.arrivals) == 10
    await transport.close()


@pytest.mark.asyncio
async def test_close_drains_paced_audio_promptly():
    socket = RecordingSocket()
    transport = _transport(socket, frames=64)
    transport.start()
    for _ in range(50):  # a full second of speech still queued
        await transport.send_audio(FRAME)
    await asyncio.sleep(0.01)

    started = asyncio.get_running_loop().time()
    await transport.close()
    elapsed = asyncio.get_running_loop().time() - started

    assert elapsed < 0.1, f"close() waited {elapsed * 1000:.0f}ms for paced audio"
    assert transport._queue.empty()


class PayloadSocket:
    """Records the decoded audio of every outbound frame, in order."""

    def __init__(self) -> None:
        self.audio: list[bytes] = []

    async def send_text(self, message: str) -> None:
        import base64

        self.audio.append(base64.b64decode(json.loads(message)["media"]["payload"]))


@pytest.mark.asyncio
async def test_queue_overflow_truncates_the_tail_instead_of_decimating():
    from smartpbx_transport import SmartPBXMediaTransport

    class SlowPayloadSocket(PayloadSocket):
        async def send_text(self, message: str) -> None:
            await asyncio.sleep(0.21)
            await super().send_text(message)

    socket = SlowPayloadSocket()
    transport = SmartPBXMediaTransport(socket, CONTEXT, max_queue_frames=8)
    transport.start()

    # TTS outruns realtime, so a long reply overflows the queue. Evicting the
    # oldest frame throws away the one that was next due, so playback jumps
    # forward again and again: garble spread through the whole reply. Refusing
    # the newest frame instead degrades to a clean cut of the tail.
    frames = [bytes([index]) * 160 for index in range(200)]
    for frame in frames:
        await transport.send_audio(frame)
    # Keep the queue full long enough for sustained-overflow behavior to run.
    await asyncio.sleep(0.6)
    sent = len(socket.audio)
    queued = transport._queue.qsize()
    dropped = transport.frames_dropped_total
    await transport.close()

    assert socket.audio, "some audio must still be delivered"
    assert socket.audio == frames[:sent], (
        "delivered audio must be a contiguous prefix of the reply, not a "
        f"decimated sample of it (got indices {[a[0] for a in socket.audio]})"
    )
    assert 0 <= (len(frames) - (sent + queued + dropped)) <= 1, (
        f"at most one frame may be in-flight during slow send_text: "
        f"{len(frames)} total frames, but {sent} sent + {queued} queued + "
        f"{dropped} dropped leaves {len(frames) - (sent + queued + dropped)} unaccounted"
    )
    assert dropped > 0, "this reply must genuinely overflow the queue"


def test_outbound_queue_holds_a_long_reply():
    from smartpbx_gateway import SmartPBXSettings

    base = {
        "ENABLE_SMARTPBX_WSS": "true", "SMARTPBX_WS_TOKEN": "token",
        "SMARTPBX_ACCOUNT_ID": "account-1",
    }
    # 512 frames of 640 bytes is ~40s of speech and ~320KB per call, so the
    # queue stops being the binding constraint on ordinary long replies.
    assert SmartPBXSettings.from_env(base).max_outbound_frames == 512
    assert SmartPBXSettings.from_env(base | {
        "SMARTPBX_MAX_OUTBOUND_FRAMES": "512",
    }).max_outbound_frames == 512
    with pytest.raises(ValueError):
        SmartPBXSettings.from_env(base | {"SMARTPBX_MAX_OUTBOUND_FRAMES": "513"})


@pytest.mark.asyncio
async def test_dropped_frames_are_counted_rather_than_silently_discarded():
    from smartpbx_transport import SmartPBXMediaTransport

    class SlowSocket(RecordingSocket):
        async def send_text(self, message: str) -> None:
            await asyncio.sleep(0.21)
            await super().send_text(message)

    notified: list[int] = []
    transport = SmartPBXMediaTransport(
        SlowSocket(), CONTEXT, max_queue_frames=2,
        on_frame_dropped=lambda: notified.append(1),
    )
    transport.start()
    # Keep overload sustained by sending faster than can be drained through
    # paced playback plus the deliberate send delay.
    for _ in range(200):
        await transport.send_audio(FRAME)
    await asyncio.sleep(1)
    await transport.close()

    assert transport.frames_dropped_total > 0, (
        "drop-oldest backpressure discarded frames with no observability at all"
    )
    assert len(notified) == transport.frames_dropped_total


def test_registry_status_surfaces_dropped_frames():
    from smartpbx_gateway import SmartPBXSessionRegistry

    registry = SmartPBXSessionRegistry(4)
    assert registry.snapshot()["frames_dropped_total"] == 0
    registry.record_frame_dropped()
    registry.record_frame_dropped()
    assert registry.snapshot()["frames_dropped_total"] == 2


@pytest.mark.asyncio
async def test_gateway_reports_dropped_frames_through_status(monkeypatch):
    import smartpbx_gateway as gateway_module

    captured: dict[str, object] = {}
    real = gateway_module.SmartPBXMediaTransport

    def spy(websocket, context, *, max_queue_frames, on_frame_dropped=None):
        captured["on_frame_dropped"] = on_frame_dropped
        return real(
            websocket, context, max_queue_frames=max_queue_frames,
            on_frame_dropped=on_frame_dropped,
        )

    monkeypatch.setattr(gateway_module, "SmartPBXMediaTransport", spy)
    registry = gateway_module.SmartPBXSessionRegistry(4)
    settings = gateway_module.SmartPBXSettings.from_env({
        "ENABLE_SMARTPBX_WSS": "true", "SMARTPBX_WS_TOKEN": "token",
        "SMARTPBX_ACCOUNT_ID": "account-1",
    })
    socket = FailingSocket(ok_sends=1000)
    socket.messages.put_nowait(json.dumps({"event": "start", "start": {
        "callId": "call-1", "otherLegCallId": "other-1",
        "callerIdNumber": "caller", "calleeIdNumber": "callee",
        "accountId": "account-1",
        "mediaFormat": {"encoding": "g711_ulaw", "sampleRate": 8000},
    }}))
    socket.messages.put_nowait(json.dumps({"event": "stop"}))

    class Session:
        def __init__(self):
            self.transfer_pending = False
            self.terminal_future = asyncio.get_running_loop().create_future()

        async def start(self):
            pass

        async def feed_audio(self, _audio):
            pass

        async def finish(self, schedule_post_call=False):
            pass

    async def factory(_context, _transport, _sink=None):
        return Session()

    gateway = gateway_module.SmartPBXGateway(settings, registry)
    await asyncio.wait_for(gateway.handle(socket, factory), timeout=5)

    assert callable(captured["on_frame_dropped"]), (
        "the gateway must give the transport somewhere to report drops"
    )
    captured["on_frame_dropped"]()
    assert gateway.snapshot()["frames_dropped_total"] == 1


@pytest.mark.asyncio
async def test_gateway_tracks_echo_rejections_through_status():
    import smartpbx_gateway as gateway_module

    settings = gateway_module.SmartPBXSettings.from_env({
        "ENABLE_SMARTPBX_WSS": "true", "SMARTPBX_WS_TOKEN": "token",
        "SMARTPBX_ACCOUNT_ID": "account-1",
    })
    socket = FailingSocket(ok_sends=1000)
    socket.messages.put_nowait(json.dumps({
        "event": "start", "start": {
            "callId": "call-1", "otherLegCallId": "other-1",
            "callerIdNumber": "caller", "calleeIdNumber": "callee",
            "accountId": "account-1",
            "mediaFormat": {"encoding": "g711_ulaw", "sampleRate": 8000},
        }
    }))
    socket.messages.put_nowait(json.dumps({"event": "stop"}))

    seen = {}

    class Session:
        def __init__(self):
            self.transfer_pending = False
            self.terminal_future = asyncio.get_running_loop().create_future()

        async def start(self):
            pass

        async def feed_audio(self, _audio):
            pass

        async def finish(self, schedule_post_call=False):
            pass

    async def factory(_context, _transport, _sink=None):
        session = Session()
        seen["session"] = session
        return session

    gateway = gateway_module.SmartPBXGateway(settings, gateway_module.SmartPBXSessionRegistry(4))
    await asyncio.wait_for(gateway.handle(socket, factory), timeout=5)

    session = seen["session"]
    assert callable(getattr(session, "_record_echo_rejection", None))
    session._record_echo_rejection(7, 0.91)
    assert gateway.snapshot()["echo_rejections_total"] == 1


@pytest.mark.asyncio
async def test_kb_retrieval_runs_off_the_event_loop(monkeypatch):
    import server

    seen: dict[str, int] = {}

    def blocking_retrieve(_text):
        seen["thread"] = threading.get_ident()
        time.sleep(0.05)
        return ""

    monkeypatch.setattr(server, "retrieve_context", blocking_retrieve)
    session = server.MediaStreamSession(
        websocket=None, lang="en", anthropic_client=None, openai_client=None,
        gemini_client=None, media_transport=None, llm_provider="claude", model="m",
    )

    async def no_llm():
        return ""

    monkeypatch.setattr(session, "_run_llm_claude", no_llm)
    await session._process_utterance_bound("do you have a room free")

    assert seen.get("thread") is not None
    assert seen["thread"] != threading.get_ident(), (
        "retrieve_context embeds with sentence-transformers and queries Chroma: "
        "tens of ms of CPU on the loop shared by every concurrent call"
    )


def test_turn_telemetry_emits_one_opaque_bounded_privacy_safe_summary():
    """A turn must be correlatable without retaining caller or utterance data."""
    import server

    emitted: list[dict[str, object]] = []
    clock = iter((
        1_000_000_000, 1_100_000_000, 1_200_000_000,
        1_350_000_000, 1_500_000_000, 2_000_000_000,
    ))
    telemetry = server.SmartPBXTurnTelemetry(
        emit=lambda _event, **fields: emitted.append(fields),
        monotonic_ns=lambda: next(clock),
        new_id=lambda: "opaque-random-turn-id",
    )

    turn_id = telemetry.start_turn("final")
    telemetry.mark(turn_id, "endpoint")
    telemetry.mark(turn_id, "kb_start")
    telemetry.mark(turn_id, "kb_complete")
    telemetry.mark_once(turn_id, "llm_first_token")
    telemetry.mark_once(turn_id, "llm_first_token")
    telemetry.finish(turn_id, "completed", generated_chars=23, delivered_sentences=2)
    telemetry.finish(turn_id, "completed")

    summary = [record for record in emitted if record["event"] == "turn_summary"]
    assert turn_id == "opaque-random-turn-id"
    assert len(summary) == 1
    assert summary[0]["turn_id"] == turn_id
    assert summary[0]["outcome"] == "completed"
    assert summary[0]["endpoint_source"] == "final"
    assert summary[0]["endpoint_ms"] == 100
    assert summary[0]["kb_ms"] == 150
    assert summary[0]["llm_first_token_ms"] == 500
    assert summary[0]["generated_chars"] == 23
    assert summary[0]["delivered_sentences"] == 2
    assert "turn_seq" not in summary[0]
    assert all(
        isinstance(value, int) and 0 <= value <= 600_000
        for key, value in summary[0].items()
        if key.endswith("_ms")
    )
    forbidden = {
        "transcript", "text", "call_id", "callSid", "caller", "phone", "payload",
        "authorization", "token", "secret", "headers", "tool_input", "exception",
    }
    assert not (forbidden & set(summary[0]))


@pytest.mark.parametrize(
    "outcome",
    ["completed", "tool_failed", "tts_failed", "interrupted", "transfer_pending"],
)
def test_turn_telemetry_summarizes_every_terminal_outcome_once(outcome):
    import server

    emitted: list[dict[str, object]] = []
    telemetry = server.SmartPBXTurnTelemetry(
        emit=lambda _event, **fields: emitted.append(fields),
        new_id=lambda: "opaque-turn",
    )
    turn_id = telemetry.start_turn("unknown")
    telemetry.finish(turn_id, outcome)

    summaries = [record for record in emitted if record["event"] == "turn_summary"]
    assert len(summaries) == 1
    assert summaries[0]["outcome"] == outcome
    assert summaries[0]["turn_id"] == "opaque-turn"


@pytest.mark.asyncio
async def test_session_finish_emits_one_opaque_aggregate_summary_after_cleanup(monkeypatch):
    import smartpbx_session

    records: list[dict[str, object]] = []
    monkeypatch.setattr(
        smartpbx_session,
        "_emit_smartpbx_session_summary",
        lambda **fields: records.append(fields),
    )
    pipeline = Pipeline()
    pipeline._turn_telemetry = types.SimpleNamespace(turns_started=3, turns_summarized=3)
    pipeline._smartpbx_barge_ins = 2
    session = _session(pipeline)
    await session.finish(False)
    await session.finish(False)

    assert records == [
        {
            "event": "session_summary",
            "session_trace_id": records[0]["session_trace_id"],
            "outcome": "finished",
            "turns_started": 3,
            "turns_summarized": 3,
            "duration_ms": records[0]["duration_ms"],
            "frames_dropped_total": 0,
            "barge_ins": 2,
        }
    ]
    assert isinstance(records[0]["session_trace_id"], str)
    assert 0 <= records[0]["duration_ms"] <= 600_000
    assert not ({"call_id", "caller", "phone", "transcript", "text"} & set(records[0]))


@pytest.mark.asyncio
async def test_transport_binds_turns_per_generation_without_false_drain_after_clear():
    from smartpbx_transport import SmartPBXMediaTransport

    websocket = FailingSocket(ok_sends=1000)
    events: list[tuple[str, int, str]] = []
    transport = SmartPBXMediaTransport(
        websocket, CONTEXT, on_transport_event=lambda turn_id, generation, stage, *_: events.append((turn_id, generation, stage)),
    )
    transport.start()
    transport.bind_turn(0, "turn-old")
    await transport.clear_audio()
    transport.bind_turn(1, "turn-new")
    await transport.send_audio(b"\xff" * 160)
    await asyncio.wait_for(transport.send_mark("tts_done"), timeout=1)
    await transport.close()

    assert events.count(("turn-new", 1, "first_media_sent")) == 1
    assert events.count(("turn-new", 1, "queue_drained")) == 1
    assert not [event for event in events if event[0] == "turn-old" and event[2] == "queue_drained"]


def _fake_google_speech(on_stream):
    """Minimal stand-in for the google-cloud-speech surface _run_one_stream uses."""

    class _AudioEncoding:
        MULAW = "MULAW"

    class RecognitionConfig:
        AudioEncoding = _AudioEncoding

        def __init__(self, **_kwargs):
            pass

    class StreamingRecognitionConfig:
        def __init__(self, **_kwargs):
            pass

    class StreamingRecognizeRequest:
        def __init__(self, **_kwargs):
            pass

    closed: list[int] = []

    class SpeechClient:
        def __init__(self):
            self.closed = False

        def streaming_recognize(self, **_kwargs):
            return on_stream()

        def close(self):
            self.closed = True
            closed.append(1)

    return (
        types.SimpleNamespace(
            RecognitionConfig=RecognitionConfig,
            StreamingRecognitionConfig=StreamingRecognitionConfig,
            StreamingRecognizeRequest=StreamingRecognizeRequest,
            SpeechClient=SpeechClient,
        ),
        closed,
    )


def test_stt_releases_its_speech_client_when_a_stream_fails(monkeypatch):
    import server

    def boom():
        raise RuntimeError("stream broke")

    fake, closed = _fake_google_speech(boom)
    monkeypatch.setattr(server, "google_speech", fake)
    stream = server.GoogleSTTStream(on_final_result=lambda *_: None, lang="en")
    stream._running = True

    with pytest.raises(RuntimeError):
        stream._run_one_stream()

    assert closed, (
        "a fresh SpeechClient and gRPC channel per ~5min stream restart, never "
        "closed, piles up fds and completion-queue threads until OOM"
    )


def test_stt_releases_its_speech_client_on_a_clean_stream(monkeypatch):
    import server

    fake, closed = _fake_google_speech(lambda: iter(()))
    monkeypatch.setattr(server, "google_speech", fake)
    stream = server.GoogleSTTStream(on_final_result=lambda *_: None, lang="en")
    stream._running = True

    stream._run_one_stream()

    assert closed


def test_stt_inbound_queue_is_bounded(monkeypatch):
    import server

    stream = server.GoogleSTTStream(on_final_result=lambda *_: None, lang="en")
    # Nothing consumes the queue: _loop was never started. An unbounded queue
    # grows for as long as the caller keeps talking.
    for _ in range(server.STT_QUEUE_MAX_CHUNKS + 500):
        stream.feed(b"\xff" * 160)

    assert stream._audio_q.qsize() <= server.STT_QUEUE_MAX_CHUNKS, (
        "a consumerless STT queue must not grow without bound"
    )


def test_stt_failure_cap_reports_a_fatal_signal(monkeypatch):
    fatal: list[int] = []
    stream, attempts, _sleeps = _failing_stt_loop(monkeypatch, on_fatal=lambda: fatal.append(1))

    assert attempts() < 10_000
    assert fatal == [1], (
        "when the restart cap trips the call must be told, or the guest sits in "
        "silence until the 90s idle timeout"
    )


@pytest.mark.asyncio
async def test_session_ends_the_call_when_stt_gives_up():
    from smartpbx_diagnostics import (
        DiagnosticFailureClass, DiagnosticOutcome, DiagnosticStage,
    )

    emitted = []

    class STT:
        def __init__(self):
            self.on_fatal = None

        def start(self):
            pass

        def stop(self):
            pass

    stt = STT()
    pipeline = Pipeline(stt)
    pipeline.lang = "en"
    session = KavyaSmartPBXSession(
        CONTEXT, Transport(), pipeline=pipeline, stt_factory=lambda **_: stt,
        post_call_processor=None, welcome_text="", llm_provider="openai", model="m",
        diagnostic_sink=lambda stage, outcome, failure: emitted.append((stage, outcome, failure)),
    )
    await session.start()

    assert callable(stt.on_fatal), "the session must wire the STT fatal signal"
    stt.on_fatal()  # invoked from the STT worker thread in production
    await asyncio.wait_for(session.terminal_future, timeout=2)

    assert emitted == [(
        DiagnosticStage.AUDIO_INGESTION,
        DiagnosticOutcome.FAILED,
        DiagnosticFailureClass.STT_UNAVAILABLE,
    )]


def _failing_stt_loop(monkeypatch, failures: int = 10_000, on_fatal=None):
    """Drive GoogleSTTStream._loop with a stream that always fails immediately."""
    import server

    stream = server.GoogleSTTStream(on_final_result=lambda *_: None, lang="en")
    stream._running = True
    if on_fatal is not None:
        stream.on_fatal = on_fatal
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
