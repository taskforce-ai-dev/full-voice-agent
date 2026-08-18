"""A callback hop already in flight at hangup must land BEFORE retention.

`_retain_pending_speech` gives every lifecycle boundary an explicit owner for
whatever is sitting in the pending buffers. That is only worth anything if the
buffers are complete when it reads them — and at teardown they are not, because
the STT providers deliver results from their own threads:

    GoogleSTTStream._on_final  ─┐
    AzureSTTStream._on_recognized ─┤ provider thread
                                 │
        MediaStreamSession._on_stt_result / _on_stt_interim
                                 │  asyncio.run_coroutine_threadsafe(...)
                                 ▼
        _accumulate_transcript / _set_transcript_interim   (event loop)

Between the `run_coroutine_threadsafe` call and the coroutine actually running
there is a queue hop the teardown path knows nothing about. A final handed over
in that window is *submitted* before teardown and *executed* after it, so
retention writes an empty buffer, the post-call snapshot is taken, and the hop
then either no-ops (SmartPBX: `_smartpbx_torn_down`) or — worse, on the Twilio
path, which has no such flag — mutates a session that is already gone.

Two guards are needed and they are NOT the same guard:

  * the ``_running`` flags inside `GoogleSTTStream` / `AzureSTTStream`, and the
    closing fence added here, stop NEW submissions;
  * the fence's DRAIN awaits submissions that were ALREADY admitted.

Neither alone is sufficient. Without the fence a callback can be submitted after
the drain finished; without the drain a callback admitted microseconds before
the fence closed is still sitting in the loop's ready queue when retention and
the snapshot run.

Determinism: a real `threading.Thread` plays the provider, sequenced against
teardown by `threading.Event` barriers — never by sleeps. The barrier is
released from `_write_audio_dump`, a real synchronous teardown step that runs
between `stt.stop()` and retention, so the submission is pinned to the exact
instant the loop cannot service it. Barrier waits are bounded so a regression
fails the assertion instead of hanging the suite.
"""

from __future__ import annotations

import asyncio
import json
import threading

import pytest

import server
from smartpbx_protocol import CallContext, MediaFormat
from smartpbx_session import KavyaSmartPBXSession


LAST_WORDS = "my number is zero seven seven one two three four five"
BARRIER_TIMEOUT = 5.0


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


class _FakeTransport:
    async def send_audio(self, _audio):
        return None

    async def clear_audio(self):
        return None

    async def send_mark(self, _name: str) -> None:
        return None

    async def close(self):
        return None


class _ThreadedSTT:
    """A real STT worker thread that delivers one final at a chosen instant.

    Constructor signature matches `_make_stt` / `KavyaSmartPBXSession`'s
    `stt_factory` so it drops into both teardown paths unchanged. `stop()`
    records the thread it ran on, which is how the off-loop-stop requirement is
    asserted: Azure's real `stop()` blocks on `.get()` of a sync future and
    Google's joins its worker thread, so running it on the event loop stalls
    every other call sharing that loop.
    """

    def __init__(
        self,
        on_final_result=None,
        on_interim_result=None,
        lang: str = "en",
        privacy_safe: bool = False,
        text: str = LAST_WORDS,
    ):
        self._on_final = on_final_result
        self._on_interim = on_interim_result
        self._text = text
        self.may_submit = threading.Event()
        self.submitted = threading.Event()
        self.stop_thread_ident: int | None = None
        self._thread: threading.Thread | None = None

    def start(self):
        self._thread = threading.Thread(
            target=self._run, name="stt-provider", daemon=True
        )
        self._thread.start()

    def _run(self):
        if not self.may_submit.wait(BARRIER_TIMEOUT):
            return
        # The provider's own thread hands the final to the event loop. This is
        # the submission the fence has to know about.
        self._on_final(self._text)
        self.submitted.set()

    def stop(self):
        self.stop_thread_ident = threading.get_ident()

    def feed(self, _audio):
        return None

    def release_and_join(self):
        """Let the worker submit its final; do not return until it has.

        Installed over `_write_audio_dump`, so the submission happens inside a
        synchronous teardown step — the loop provably cannot run the queued hop
        before teardown reaches retention.
        """
        self.may_submit.set()
        self.submitted.wait(BARRIER_TIMEOUT)
        if self._thread is not None:
            self._thread.join(timeout=BARRIER_TIMEOUT)


class _BlockingThreadedSTT(_ThreadedSTT):
    """Stops off-loop, but only after the test releases its deterministic gate."""

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.stop_started = threading.Event()
        self.stop_may_finish = threading.Event()

    def stop(self):
        self.stop_thread_ident = threading.get_ident()
        self.stop_started.set()
        self.stop_may_finish.wait(BARRIER_TIMEOUT)


class _TwoFinalThreadedSTT(_ThreadedSTT):
    """Delivers two material provider finals with identical text in order."""

    def _run(self):
        if not self.may_submit.wait(BARRIER_TIMEOUT):
            return
        self._on_final(self._text)
        self._on_final(self._text)
        self.submitted.set()


def make_live_session(*, smartpbx=True, lang="en"):
    """A session bound to the REAL running loop — threads must submit into it."""
    session = server.MediaStreamSession(websocket=None, lang=lang, media_transport=None)
    if smartpbx:
        session._smartpbx_transfer_context = object()
        session._media_transport = _FakeTransport()
    session._event_loop = asyncio.get_running_loop()
    processed: list[str] = []

    async def record(text):
        processed.append(text)

    session._process_utterance = record

    async def _noop(*_args, **_kwargs):
        return None

    session._clear_media_audio = _noop
    return session, processed


def _context() -> CallContext:
    return CallContext(
        call_id="dialog-media-leg",
        other_leg_call_id="dialog-safe-call",
        caller_id_number="+94000000000",
        callee_id_number="+94110000000",
        account_id="dialog-account",
        media_format=MediaFormat(encoding="g711_ulaw", sample_rate=8000),
    )


def _all_texts(transcript) -> list[str]:
    """Role-agnostic on purpose: these tests are about ARRIVAL, not schema."""
    return [entry.get("text") for entry in transcript]


async def _settle(times: int = 4) -> None:
    """Give any hop that escaped the fence every chance to land."""
    for _ in range(times):
        await asyncio.sleep(0)


async def _wait_for_thread_event(event: threading.Event) -> None:
    assert await asyncio.to_thread(event.wait, BARRIER_TIMEOUT)


async def _held_flush(session, release: asyncio.Event) -> None:
    """A flush task created before teardown, released while stop is pending."""
    await release.wait()
    await session._flush_transcript()


# ---------------------------------------------------------------------------
# 1. SmartPBX teardown (`_finish_once` -> `_finalize_smartpbx_turns`)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_smartpbx_hangup_retains_a_final_submitted_from_the_worker_thread():
    """The headline regression: a final in flight at hangup must not vanish."""
    session, processed = make_live_session()
    stt = _ThreadedSTT(on_final_result=session._on_stt_result)
    session._stt = stt
    stt.start()
    session._write_audio_dump = stt.release_and_join

    seen: list[list[dict[str, str]]] = []

    async def process_post_call(**metadata):
        seen.append(list(metadata["full_transcript"]))

    dialog_session = KavyaSmartPBXSession(
        _context(),
        _FakeTransport(),
        pipeline=session,
        stt_factory=lambda **_kwargs: None,
        post_call_processor=process_post_call,
        welcome_text="",
        llm_provider="claude",
        model="test-model",
    )

    await dialog_session.finish(schedule_post_call=True)
    await _settle()

    assert LAST_WORDS in _all_texts(session.full_transcript), (
        "a final submitted from the STT thread before the fence closed must be "
        "drained into the buffers before retention reads them"
    )
    assert _all_texts(session.full_transcript).count(LAST_WORDS) == 1, (
        "handled exactly once — drained, retained, and never replayed"
    )
    assert seen, "post-call extraction must still be scheduled"
    assert LAST_WORDS in _all_texts(seen[0]), (
        "and the snapshot it receives must already contain the drained final"
    )
    assert processed == [], "a hangup must never start a fresh LLM turn"
    assert session._endpointing_handle is None, (
        "no endpointing timer may outlive teardown"
    )
    assert session._pending_transcript == ""
    assert session._committed_transcript == ""


@pytest.mark.asyncio
async def test_smartpbx_drain_completes_before_retention_and_snapshot():
    """Item 4's ordering, pinned explicitly: drain -> retain -> snapshot."""
    session, _processed = make_live_session()
    stt = _ThreadedSTT(on_final_result=session._on_stt_result)
    session._stt = stt
    stt.start()
    session._write_audio_dump = stt.release_and_join

    order: list[str] = []

    accumulate = session._accumulate_transcript

    async def traced_accumulate(text):
        order.append("callback")
        await accumulate(text)

    session._accumulate_transcript = traced_accumulate

    retain = session._retain_pending_speech

    async def traced_retain(reason):
        order.append(f"retain:{reason}")
        await retain(reason)

    session._retain_pending_speech = traced_retain

    async def process_post_call(**_metadata):
        order.append("snapshot")

    dialog_session = KavyaSmartPBXSession(
        _context(),
        _FakeTransport(),
        pipeline=session,
        stt_factory=lambda **_kwargs: None,
        post_call_processor=process_post_call,
        welcome_text="",
        llm_provider="claude",
        model="test-model",
    )

    await dialog_session.finish(schedule_post_call=True)
    await _settle()

    assert order == ["callback", "retain:hangup", "snapshot"], (
        "the in-flight callback must be drained BEFORE retention, and retention "
        "before the post-call snapshot; got " + repr(order)
    )


@pytest.mark.asyncio
async def test_smartpbx_teardown_retains_identical_material_finals_in_order():
    """Text equality is not provider-result identity: "yes, yes" is two finals."""
    session, processed = make_live_session()
    stt = _TwoFinalThreadedSTT(
        on_final_result=session._on_stt_result,
        text="yes",
    )
    session._stt = stt
    stt.start()
    session._write_audio_dump = stt.release_and_join
    dialog_session = KavyaSmartPBXSession(
        _context(), _FakeTransport(), pipeline=session,
        stt_factory=lambda **_kwargs: None, post_call_processor=None,
        welcome_text="", llm_provider="claude", model="test-model",
    )

    await dialog_session.finish(schedule_post_call=False)
    await _settle()

    assert _all_texts(session.full_transcript) == ["yes yes"]
    assert processed == []


@pytest.mark.asyncio
async def test_smartpbx_teardown_refuses_a_submission_made_after_the_fence_closed():
    """The other half of the fence: nothing may be submitted once it is closed."""
    session, processed = make_live_session()
    stt = _ThreadedSTT(on_final_result=session._on_stt_result)
    session._stt = stt

    dialog_session = KavyaSmartPBXSession(
        _context(),
        _FakeTransport(),
        pipeline=session,
        stt_factory=lambda **_kwargs: None,
        post_call_processor=None,
        welcome_text="",
        llm_provider="claude",
        model="test-model",
    )

    await dialog_session.finish(schedule_post_call=False)
    await _settle()

    before = list(session.full_transcript)

    # A residual provider thread fires after teardown has completed.
    stt.start()
    stt.release_and_join()
    await _settle()

    assert list(session.full_transcript) == before, (
        "a callback submitted after the fence closed must not reach the transcript"
    )
    assert session._pending_transcript == "", (
        "nor may it mutate the pending buffers of a session that is already gone"
    )
    assert session._endpointing_handle is None, (
        "nor arm an endpointing timer after teardown"
    )
    assert processed == []


@pytest.mark.asyncio
async def test_smartpbx_teardown_closes_turn_dispatch_before_awaiting_stt_stop():
    """A pre-created flush may not start a turn while off-loop stop is pending."""
    session, processed = make_live_session()
    session._pending_transcript = "pre-teardown endpoint task"
    release_flush = asyncio.Event()
    flush_task = asyncio.create_task(_held_flush(session, release_flush))
    stt = _BlockingThreadedSTT(on_final_result=session._on_stt_result)
    session._stt = stt
    stt.start()
    session._write_audio_dump = stt.release_and_join

    dialog_session = KavyaSmartPBXSession(
        _context(), _FakeTransport(), pipeline=session,
        stt_factory=lambda **_kwargs: None, post_call_processor=None,
        welcome_text="", llm_provider="claude", model="test-model",
    )
    finish_task = asyncio.create_task(dialog_session.finish(schedule_post_call=False))
    await _wait_for_thread_event(stt.stop_started)
    release_flush.set()
    await _settle()
    stt.stop_may_finish.set()
    await finish_task
    await flush_task
    await _settle()

    assert processed == [], "teardown entry must fence dispatch before its first await"
    assert LAST_WORDS in _all_texts(session.full_transcript)
    assert session._endpointing_handle is None


@pytest.mark.asyncio
async def test_stt_callback_drain_times_out_without_speech_or_exception_leakage(caplog):
    """The fixed drain timeout reports only allowlisted outcome telemetry.

    Contract: `stt_callback_drain` telemetry exposes only `outcome`, `pending`,
    and bounded timing; retained-speech telemetry separately exposes exactly
    reason/provenance/answered/chars with the hard 1000-character cap.
    """
    session, _processed = make_live_session()
    secret = "SENTINEL-UNRETAINED-SPEECH"

    async def stalled():
        await asyncio.Event().wait()

    assert session._submit_stt_callback(stalled)
    with caplog.at_level("INFO"):
        await asyncio.wait_for(
            session._close_stt_callbacks(),
            timeout=server.STT_CALLBACK_DRAIN_TIMEOUT_SECONDS * 2,
        )

    line = next(
        record.getMessage()
        for record in caplog.records
        if "event=stt_callback_drain" in record.getMessage()
    )
    assert "outcome=timeout" in line
    assert secret not in line
    assert "RuntimeError" not in line


@pytest.mark.asyncio
async def test_stt_callback_drain_reports_raising_callback_without_swallowing_it(caplog):
    """A raised admitted callback is observable but cannot block teardown."""
    session, _processed = make_live_session()

    async def raises():
        raise RuntimeError("SENTINEL-CALLBACK-ERROR")

    assert session._submit_stt_callback(raises)
    with caplog.at_level("INFO"):
        await session._close_stt_callbacks()

    line = next(
        record.getMessage()
        for record in caplog.records
        if "event=stt_callback_drain" in record.getMessage()
    )
    assert "outcome=error" in line
    assert "SENTINEL-CALLBACK-ERROR" not in line


# ---------------------------------------------------------------------------
# 2. Twilio Media Streams teardown (`run()` finally)
# ---------------------------------------------------------------------------


class _FakeWebSocket:
    """Yields exactly one `stop` event, then run() must unwind."""

    def __init__(self):
        self.accepted = False
        self._sent = False

    async def accept(self):
        self.accepted = True

    async def receive_text(self):
        if not self._sent:
            self._sent = True
            return json.dumps({"event": "stop"})
        raise AssertionError("run() must stop after the stop event")


@pytest.mark.asyncio
async def test_twilio_teardown_stops_stt_off_the_event_loop(monkeypatch):
    """Azure's stop() blocks on .get(); Google's joins a thread. Not on the loop."""
    session, _processed = make_live_session(smartpbx=False, lang="si")
    session.ws = _FakeWebSocket()
    loop_ident = threading.get_ident()

    created: dict[str, _ThreadedSTT] = {}

    def factory(**kwargs):
        stt = _ThreadedSTT(**kwargs)
        created["stt"] = stt
        return stt

    monkeypatch.setattr(server, "_make_stt", factory)
    monkeypatch.setattr(server, "process_post_call_data", lambda **_kwargs: None)

    await session.run()

    stt = created["stt"]
    assert stt.stop_thread_ident is not None, "stop() must actually be called"
    assert stt.stop_thread_ident != loop_ident, (
        "stt.stop() blocks — running it on the shared event loop stalls every "
        "other concurrent call; it must run off-loop"
    )


@pytest.mark.asyncio
async def test_twilio_teardown_retains_a_final_submitted_from_the_worker_thread(
    monkeypatch,
):
    """Same regression on the Twilio path, which has no `_smartpbx_torn_down`."""
    session, processed = make_live_session(smartpbx=False, lang="si")
    session.ws = _FakeWebSocket()

    created: dict[str, _ThreadedSTT] = {}

    def factory(**kwargs):
        stt = _ThreadedSTT(**kwargs)
        created["stt"] = stt
        # `MediaStreamSession.run()` owns start(); starting here too created a
        # second provider worker and made a harness artifact look like a replay.
        session._write_audio_dump = stt.release_and_join
        return stt

    monkeypatch.setattr(server, "_make_stt", factory)

    seen: list[list[dict[str, str]]] = []

    async def process_post_call(**metadata):
        seen.append(list(metadata["full_transcript"]))

    monkeypatch.setattr(server, "process_post_call_data", process_post_call)

    await session.run()
    await _settle()

    assert LAST_WORDS in _all_texts(session.full_transcript), (
        "a final submitted from the STT thread before the Twilio stream tore "
        "down must be drained into the buffers before retention"
    )
    assert _all_texts(session.full_transcript).count(LAST_WORDS) == 1
    assert seen, "post-call extraction must be scheduled — the transcript is not empty"
    assert LAST_WORDS in _all_texts(seen[0])
    assert processed == [], "the stop event must never start a fresh LLM turn"
    assert session._endpointing_handle is None, (
        "the Twilio path has no torn-down flag: a late callback that arms an "
        "endpointing timer here leaves a live timer on a dead session"
    )
    assert session._pending_transcript == ""
    assert session._committed_transcript == ""


@pytest.mark.asyncio
async def test_twilio_teardown_closes_turn_dispatch_before_awaiting_stt_stop(monkeypatch):
    """Twilio shares SmartPBX's early dispatch fence and late callback drain."""
    session, processed = make_live_session(smartpbx=False, lang="si")
    session.ws = _FakeWebSocket()
    session._pending_transcript = "pre-teardown endpoint task"
    release_flush = asyncio.Event()
    flush_task = asyncio.create_task(_held_flush(session, release_flush))
    created: dict[str, _BlockingThreadedSTT] = {}

    def factory(**kwargs):
        stt = _BlockingThreadedSTT(**kwargs)
        created["stt"] = stt
        # `MediaStreamSession.run()` owns start(); starting here too created a
        # second provider worker and made a harness artifact look like a replay.
        session._write_audio_dump = stt.release_and_join
        return stt

    monkeypatch.setattr(server, "_make_stt", factory)
    monkeypatch.setattr(server, "process_post_call_data", lambda **_kwargs: None)
    run_task = asyncio.create_task(session.run())
    while "stt" not in created:
        await asyncio.sleep(0)
    stt = created["stt"]
    await _wait_for_thread_event(stt.stop_started)
    release_flush.set()
    await _settle()
    stt.stop_may_finish.set()
    await run_task
    await flush_task
    await _settle()

    assert processed == [], "a pre-created flush cannot start an LLM turn at hangup"
    assert _all_texts(session.full_transcript).count(LAST_WORDS) == 1, (
        "the one admitted worker final must survive exactly once"
    )
    assert session._endpointing_handle is None
