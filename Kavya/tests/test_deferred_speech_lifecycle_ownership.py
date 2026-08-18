"""Deferred caller speech must have an OWNER at every lifecycle boundary.

Narrowing the post-dispatch predicate (see
`test_stt_post_dispatch_material_admission.py`) means far more speech is now
admitted while a turn already owns the endpoint: it sits in
`_pending_transcript` / `_committed_transcript` with either an armed endpointing
timer or `_deferred_flush_pending` set, waiting for the turn to release the
guard. Admitting speech and then dropping it at teardown would be the same
silent loss, moved later — so every boundary that can end or divert the call
while that buffer is pending must declare what happens to it.

Three ownerships are possible and exactly one applies per boundary:

  DISPATCHED  the pending speech becomes a turn.
  RETAINED    it does not become a turn, but it reaches `full_transcript`, and
              therefore the call log and post-call extraction — the
              `_force_pending_capture_dispatch` precedent, which already does
              this for a half-dictated number.
  TRANSFERRED it travels with the transfer context.

Never a silent clear. The decisions pinned here:

  * BARGE-IN — SUPERSEDED for dispatch, RETAINED for the record. The guest is
    speaking NEW content right now, and that utterance (not the older buffer) is
    what the next turn must answer; prepending stale text would answer a question
    the guest has moved on from. But the older text was still something the guest
    said and was never answered, so it is written to `full_transcript` before the
    buffer is cleared. Supersession is a dispatch decision, not a licence to lose
    words.
  * TRANSFER-PENDING — RETAINED. After the hand-off nothing in this process will
    ever record it again, and the transcript is what travels onward.
  * SMARTPBX TEARDOWN (`_finish_once`) — RETAINED, before the post-call snapshot.
  * TWILIO MEDIA STREAMS TEARDOWN (`run()` finally) — RETAINED, before the
    post-call task is created.

Determinism: `Event` barriers hold the turn open, `FakeLoop` captures endpointing
timers, and the two teardown paths are driven through their real entry points.
"""

from __future__ import annotations

import asyncio
import json

import pytest

import server
from smartpbx_protocol import CallContext, MediaFormat
from smartpbx_session import KavyaSmartPBXSession


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


class _Scheduled:
    def __init__(self, delay, callback):
        self.delay = delay
        self.callback = callback
        self.cancelled = False

    def cancel(self):
        self.cancelled = True


class FakeLoop:
    def __init__(self):
        self.scheduled: list[_Scheduled] = []

    def call_later(self, delay, callback):
        handle = _Scheduled(delay, callback)
        self.scheduled.append(handle)
        return handle

    @property
    def last(self) -> _Scheduled:
        return self.scheduled[-1]

    @property
    def live(self) -> list[_Scheduled]:
        return [handle for handle in self.scheduled if not handle.cancelled]


class _FakeTransport:
    def __init__(self):
        self.marks: list[str] = []

    async def send_audio(self, _audio):
        return None

    async def clear_audio(self):
        return None

    async def send_mark(self, name: str) -> None:
        self.marks.append(name)

    async def close(self):
        return None


async def _noop(*_args, **_kwargs):
    return None


DISPATCHED = "i would like a room for two nights"
DEFERRED = "and could we have a late checkout as well"


def make_session(*, hold=None, smartpbx=True, lang="en"):
    session = server.MediaStreamSession(websocket=None, lang=lang, media_transport=None)
    if smartpbx:
        session._smartpbx_transfer_context = object()
        session._media_transport = _FakeTransport()
    loop = FakeLoop()
    session._event_loop = loop
    processed: list[str] = []

    async def record(text):
        processed.append(text)
        if hold is not None:
            await hold.wait()

    session._process_utterance = record
    session._clear_media_audio = _noop
    return session, loop, processed


async def _dispatch_turn(session, loop, text=DISPATCHED):
    await session._accumulate_transcript(text)
    loop.last.callback()
    await asyncio.sleep(0)
    assert session._utterance_dispatched is True, "turn must be in flight"


def _context() -> CallContext:
    return CallContext(
        call_id="dialog-media-leg",
        other_leg_call_id="dialog-safe-call",
        caller_id_number="+94000000000",
        callee_id_number="+94110000000",
        account_id="dialog-account",
        media_format=MediaFormat(encoding="g711_ulaw", sample_rate=8000),
    )


def _texts(transcript) -> list[str]:
    return [
        entry["text"]
        for entry in transcript
        if entry.get("role") in {"user", server.RETAINED_SPEECH_ROLE}
    ]


# ---------------------------------------------------------------------------
# 1. barge-in — SUPERSEDED for dispatch, RETAINED for the record
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_barge_in_retains_superseded_pending_speech_in_the_transcript():
    """The new utterance supersedes the buffer; the buffer still reaches history."""
    hold = asyncio.Event()
    session, loop, processed = make_session(hold=hold)
    await _dispatch_turn(session, loop)

    await session._accumulate_transcript(DEFERRED)
    assert session._pending_transcript == DEFERRED, "precondition: speech is pending"

    session._is_speaking = True
    await session._handle_bargein()

    assert DEFERRED in _texts(session.full_transcript), (
        "speech admitted during the turn must not vanish when the guest interrupts"
    )
    # Supersession, asserted explicitly: it does NOT become a turn, and the
    # interrupting utterance starts from a clean buffer.
    assert processed == [DISPATCHED], "the superseded buffer must not dispatch"
    assert session._pending_transcript == ""
    assert session._committed_transcript == ""
    assert session._latest_interim == ""
    assert session._deferred_flush_pending is False

    hold.set()
    await asyncio.sleep(0)


@pytest.mark.asyncio
async def test_barge_in_retains_speech_whose_deferred_flush_was_already_armed():
    """The stranded-buffer case: the deadline fired inside the turn, then a barge-in."""
    hold = asyncio.Event()
    session, loop, processed = make_session(hold=hold)
    await _dispatch_turn(session, loop)

    await session._accumulate_transcript(DEFERRED)
    loop.last.callback()
    await asyncio.sleep(0)
    assert session._deferred_flush_pending is True, "precondition: flush was deferred"

    session._is_speaking = True
    await session._handle_bargein()

    assert DEFERRED in _texts(session.full_transcript), (
        "speech whose deferred flush was armed must not vanish at barge-in"
    )
    assert session._deferred_flush_pending is False
    assert processed == [DISPATCHED]

    hold.set()
    await asyncio.sleep(0)


# ---------------------------------------------------------------------------
# 2. transfer-pending — RETAINED
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_transfer_pending_retains_pending_speech_in_the_transcript():
    """`enter_transfer_pending` clears the buffers — it must record them first."""
    session, _loop, _processed = make_session()
    await session._accumulate_transcript(DEFERRED)
    assert session._is_capture_mode_active() is False, (
        "precondition: ordinary speech, not a capture dictation"
    )

    await session.enter_transfer_pending()

    assert _texts(session.full_transcript) == [DEFERRED], (
        "pending speech must reach the transcript that travels with the transfer"
    )
    assert session._pending_transcript == ""
    assert session._committed_transcript == ""
    assert session.transfer_pending is True


@pytest.mark.asyncio
async def test_flush_during_transfer_pending_retains_pending_speech():
    """The flush's transfer branch is the other place the buffer is dropped."""
    session, _loop, processed = make_session()
    await session._accumulate_transcript(DEFERRED)
    session.transfer_pending = True

    await session._flush_transcript()

    assert _texts(session.full_transcript) == [DEFERRED], (
        "a flush landing after the transfer began must record, not discard"
    )
    assert processed == [], "it must not become a turn once the transfer began"
    assert session._pending_transcript == ""
    assert session._committed_transcript == ""
    assert session._deferred_flush_pending is False


# ---------------------------------------------------------------------------
# 3. SmartPBX teardown (`_finish_once`) — RETAINED
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_smartpbx_teardown_retains_pending_speech_for_post_call():
    session, _loop, _processed = make_session()
    await session._accumulate_transcript(DEFERRED)

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
    await asyncio.sleep(0)

    assert seen and _texts(seen[0]) == [DEFERRED], (
        "speech still buffered at Dialog hangup must reach post-call extraction"
    )


# ---------------------------------------------------------------------------
# 4. Twilio Media Streams teardown (`run()` finally) — RETAINED
# ---------------------------------------------------------------------------


class _StubSTT:
    def __init__(self, **_kwargs):
        self.started = False

    def start(self):
        self.started = True

    def stop(self):
        self.started = False

    def feed(self, _audio):
        return None


class _FakeWebSocket:
    """Yields one `stop` event; the buffer is seeded just before it arrives."""

    def __init__(self, session, text):
        self._session = session
        self._text = text
        self.accepted = False
        self._sent = False

    async def accept(self):
        self.accepted = True

    async def receive_text(self):
        if not self._sent:
            self._sent = True
            self._session._pending_transcript = self._text
            self._session._committed_transcript = self._text
            return json.dumps({"event": "stop"})
        raise AssertionError("run() must stop after the stop event")


@pytest.mark.asyncio
async def test_twilio_run_teardown_retains_pending_speech_for_post_call(monkeypatch):
    session, _loop, _processed = make_session(smartpbx=False, lang="si")
    session.ws = _FakeWebSocket(session, DEFERRED)

    monkeypatch.setattr(server, "_make_stt", lambda **_kwargs: _StubSTT())

    seen: list[list[dict[str, str]]] = []

    async def process_post_call(**metadata):
        seen.append(list(metadata["full_transcript"]))

    monkeypatch.setattr(server, "process_post_call_data", process_post_call)

    await session.run()
    await asyncio.sleep(0)

    assert _texts(session.full_transcript) == [DEFERRED], (
        "speech still buffered when the Twilio stream stops must reach the transcript"
    )
    assert seen and _texts(seen[0]) == [DEFERRED], (
        "and must be visible to post-call extraction"
    )
