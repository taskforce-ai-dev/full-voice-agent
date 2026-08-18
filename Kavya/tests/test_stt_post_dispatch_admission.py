"""Post-dispatch STT results: reject only what is PROVEN stale, queue the rest.

`test_stt_late_result_isolation.py` gave a dispatched turn ownership of the STT
endpoint. That gate refused EVERY final and interim while `_utterance_dispatched`
was True — including genuine new caller speech arriving while `_is_speaking` is
False: during pre-TTS LLM/tool latency, and in the gaps between delivered
sentences of one reply. That speech was silently lost: no buffer, no timer, no
turn, no history, nothing but one telemetry line saying a result was ignored.

The policy this file pins is turn-scoped and text-relational:

  * PROVEN stale/duplicate — the result, normalised for whitespace and case, is
    the dispatched utterance itself, a token-boundary prefix of it, or a superset
    of it that adds no material characters — AND it arrived inside the short
    staleness window after dispatch. Refused before any counter, buffer or timer
    changes, with the existing bounded telemetry retained.
  * Anything else is UNPROVEN and is admitted: it accumulates into the pending
    buffers, cancels and resets the silence re-prompt, and is dispatched as the
    NEXT turn once the active turn releases the exactly-once guard. It never
    joins the running turn's history or telemetry.

No fuzzy matching anywhere: exact normalised comparison only.

Determinism: `Event` barriers hold the turn open and `FakeLoop` captures
endpointing timers instead of awaiting them — never a wall-clock sleep.

Tests marked "preservation guard" hold in BOTH the pre-fix and post-fix trees on
purpose: the tail/duplicate rejection, its reject-before-counters ordering, its
privacy-safe telemetry and its timer-freedom must all survive the admission path.
"""

from __future__ import annotations

import asyncio
import re
import time

import pytest

import server


# ---------------------------------------------------------------------------
# Fixtures — same shape as test_stt_late_result_isolation.py
# ---------------------------------------------------------------------------


class _Scheduled:
    def __init__(self, delay, callback):
        self.delay = delay
        self.callback = callback
        self.cancelled = False

    def cancel(self):
        self.cancelled = True


class FakeLoop:
    """Captures call_later so timer arming is asserted, never awaited."""

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
        return [h for h in self.scheduled if not h.cancelled]


class _FakeTransport:
    """Minimal media transport: enough for the real `_send_tts_done` to run."""

    def __init__(self):
        self.marks: list[str] = []

    async def send_mark(self, name: str) -> None:
        self.marks.append(name)


async def _noop(*_args, **_kwargs):
    return None


POST_DISPATCH_EVENT = "smartpbx_media event=stt_post_dispatch_result"
DISPATCHED = "i would like a room for two nights"
NEW_SPEECH = "actually can you also add breakfast"


def make_session(*, hold=None, smartpbx=True, lang="en", telemetry=None):
    """A direct-SmartPBX-English session whose LLM turn is recorded, not run.

    `hold` (an `asyncio.Event`) blocks inside `_process_utterance`, holding the
    dispatched turn open so results land in the exact pre-TTS window the
    production defect occurs in. `_is_speaking` stays False throughout: this is
    the LLM/tool gap and the inter-sentence gap, not speaking time.
    """
    session = server.MediaStreamSession(websocket=None, lang=lang, media_transport=None)
    if smartpbx:
        session._smartpbx_transfer_context = object()
        session._media_transport = _FakeTransport()
    if telemetry is not None:
        session._turn_telemetry = telemetry
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
    """Dispatch one turn and leave it in flight (held inside the runner)."""
    await session._accumulate_transcript(text)
    loop.last.callback()
    await asyncio.sleep(0)
    assert session._utterance_dispatched is True, "turn must be in flight"
    assert session._is_speaking is False, "this is the pre-TTS / inter-sentence gap"


async def _release(session, hold, times: int = 40):
    hold.set()
    for _ in range(times):
        await asyncio.sleep(0)
        if session._utterance_dispatched is False:
            return
    raise AssertionError("the held turn never released the dispatch guard")


def _fire_live(loop):
    for handle in loop.live:
        handle.callback()


def _post_dispatch_records(caplog):
    return [
        record.getMessage()
        for record in caplog.records
        if record.getMessage().startswith(POST_DISPATCH_EVENT)
    ]


def _summaries(emitted):
    return [record for record in emitted if record.get("event") == "turn_summary"]


def _recording_telemetry(emitted, turn_id="opaque-turn-id"):
    return server.SmartPBXTurnTelemetry(
        emit=lambda _event, **fields: emitted.append(fields),
        new_id=lambda: turn_id,
    )


POST_DISPATCH_SUMMARY_FIELDS = (
    "ignored_post_dispatch_finals",
    "ignored_post_dispatch_interims",
    "ignored_post_dispatch_max_elapsed_ms",
)


# ---------------------------------------------------------------------------
# 1. genuine new speech in the pre-TTS gap survives
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_genuine_new_final_in_a_pre_tts_gap_is_not_silently_lost():
    """The reviewer's scenario: new speech while the runner holds the turn."""
    hold = asyncio.Event()
    session, loop, processed = make_session(hold=hold)
    await _dispatch_turn(session, loop)

    timers = len(loop.scheduled)
    await session._accumulate_transcript(NEW_SPEECH)

    assert session._committed_transcript == NEW_SPEECH, (
        "genuine new caller speech was discarded by the post-dispatch gate"
    )
    assert session._pending_transcript == NEW_SPEECH
    assert len(loop.scheduled) == timers + 1, (
        "admitted speech must arm endpointing so it can become a turn"
    )
    assert processed == [DISPATCHED], "the running turn must not be re-entered"

    await _release(session, hold)


@pytest.mark.asyncio
async def test_genuine_new_interim_in_a_pre_tts_gap_is_not_silently_lost():
    hold = asyncio.Event()
    session, loop, processed = make_session(hold=hold)
    await _dispatch_turn(session, loop)

    timers = len(loop.scheduled)
    await session._set_transcript_interim("actually could we")

    assert session._pending_transcript == "actually could we", (
        "a genuine new interim was discarded by the post-dispatch gate"
    )
    assert session._latest_interim == "actually could we"
    assert len(loop.scheduled) == timers + 1
    assert processed == [DISPATCHED]

    await _release(session, hold)


@pytest.mark.asyncio
async def test_genuine_new_speech_cancels_and_resets_the_silence_reprompt():
    """Between delivered sentences the nudge is armed; new speech must kill it."""
    hold = asyncio.Event()
    session, loop, _processed = make_session(hold=hold)
    await _dispatch_turn(session, loop)

    # The real delivered-sentence path arms the real re-prompt task.
    delivered = await session._send_tts_done(sentence="One moment.")
    assert delivered is True
    armed = session._reprompt_task
    assert armed is not None, "a delivered sentence must arm the nudge"
    session._reprompt_count = 1

    await session._accumulate_transcript(NEW_SPEECH)
    await asyncio.sleep(0)

    assert session._reprompt_task is None, (
        "genuine new caller speech must cancel the pending silence nudge"
    )
    assert armed.cancelled() or armed.done()
    assert session._reprompt_count == 0, (
        "genuine new caller speech must reset the re-prompt attempt counter"
    )

    session._cancel_reprompt()
    await _release(session, hold)


@pytest.mark.asyncio
async def test_genuine_new_speech_becomes_the_next_turn_after_the_guard_releases():
    """The deferred flush must actually dispatch once the turn lets go."""
    hold = asyncio.Event()
    session, loop, processed = make_session(hold=hold)
    await _dispatch_turn(session, loop)

    await session._accumulate_transcript(NEW_SPEECH)
    # The endpointing deadline expires INSIDE the active turn: `_flush_transcript`
    # defers on the exactly-once guard. The buffer must not be stranded there.
    loop.last.callback()
    await asyncio.sleep(0)
    assert processed == [DISPATCHED], "the queued speech must not join the old turn"
    assert session._pending_transcript == NEW_SPEECH, "the buffer must survive"

    await _release(session, hold)
    _fire_live(loop)
    await asyncio.sleep(0)
    await asyncio.sleep(0)

    assert processed == [DISPATCHED, NEW_SPEECH], (
        "speech deferred by the active turn must dispatch once the guard releases"
    )
    assert session.full_transcript == [
        {"role": "user", "text": DISPATCHED},
        {"role": "user", "text": NEW_SPEECH},
    ]
    assert session._pending_transcript == ""
    assert session._committed_transcript == ""


@pytest.mark.asyncio
async def test_queued_speech_does_not_contaminate_the_running_turn(caplog):
    """No history, no telemetry, no turn id of the old turn may move."""
    emitted: list[dict[str, object]] = []
    hold = asyncio.Event()
    session, loop, processed = make_session(
        hold=hold, telemetry=_recording_telemetry(emitted)
    )
    await _dispatch_turn(session, loop)
    turn_id = session._active_smartpbx_turn_id
    history = list(session.full_transcript)

    with caplog.at_level("INFO"):
        await session._accumulate_transcript(NEW_SPEECH)
        await session._set_transcript_interim(NEW_SPEECH + " please")

    assert session.full_transcript == history, "the old turn's history must not move"
    assert session._active_smartpbx_turn_id == turn_id
    assert processed == [DISPATCHED]
    assert _post_dispatch_records(caplog) == [], (
        "admitted speech is not an ignored result and must not be recorded as one"
    )

    session._finalize_smartpbx_turns()
    summary = _summaries(emitted)[-1]
    assert summary["turn_id"] == turn_id
    for field in POST_DISPATCH_SUMMARY_FIELDS:
        assert field not in summary, field

    await _release(session, hold)


@pytest.mark.asyncio
async def test_cumulative_superset_that_adds_material_content_is_admitted():
    """A tail that ADDS words is not proven stale — it may be new speech."""
    hold = asyncio.Event()
    session, loop, _processed = make_session(hold=hold)
    await _dispatch_turn(session, loop)

    await session._accumulate_transcript(DISPATCHED + " and a late checkout")

    assert session._pending_transcript == DISPATCHED + " and a late checkout", (
        "a superset carrying new words must not be treated as a duplicate"
    )

    await _release(session, hold)


@pytest.mark.asyncio
async def test_an_echo_of_the_dispatched_text_outside_the_stale_window_is_admitted():
    """Recency is part of the proof: an old echo is a repeat, not a tail."""
    hold = asyncio.Event()
    session, loop, _processed = make_session(hold=hold)
    await _dispatch_turn(session, loop)

    # Far outside any plausible provider-tail window (the policy's own window is
    # a couple of seconds), without pinning the constant from the test.
    session._utterance_dispatched_at = time.monotonic() - 30.0
    await session._accumulate_transcript(DISPATCHED)

    assert session._pending_transcript == DISPATCHED, (
        "a caller repeating themselves long after dispatch is new speech"
    )

    await _release(session, hold)


@pytest.mark.asyncio
async def test_twilio_media_streams_session_queues_genuine_new_speech_too(caplog):
    """The policy is SHARED with the Twilio Media Streams path, deliberately.

    ConversationRelay never reaches this accumulator; Twilio Media Streams uses
    exactly the same one, and the stale-tail hazard is provider-level rather
    than transport-level. Same admission, same queueing, and still no SmartPBX
    log vocabulary on a Twilio call.
    """
    hold = asyncio.Event()
    session, loop, processed = make_session(hold=hold, smartpbx=False, lang="si")
    assert session._is_smartpbx_session() is False

    await _dispatch_turn(session, loop, "mata kamarayak one")

    with caplog.at_level("INFO"):
        await session._accumulate_transcript("thawa dawasak wadi karanna")
        loop.last.callback()
        await asyncio.sleep(0)

    assert processed == ["mata kamarayak one"]
    await _release(session, hold)
    _fire_live(loop)
    await asyncio.sleep(0)
    await asyncio.sleep(0)

    assert processed == ["mata kamarayak one", "thawa dawasak wadi karanna"], (
        "Twilio Media Streams speech in the gap must queue, not vanish"
    )
    assert _post_dispatch_records(caplog) == []
    assert "smartpbx_media" not in caplog.text


# ---------------------------------------------------------------------------
# 2. proven stale/duplicate results are still refused (preservation guards)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_exact_duplicate_tail_is_still_rejected():
    """Preservation guard: the provider's own re-send of the dispatched text."""
    hold = asyncio.Event()
    session, loop, processed = make_session(hold=hold)
    await _dispatch_turn(session, loop)

    timers = len(loop.scheduled)
    await session._accumulate_transcript(DISPATCHED)

    assert session._committed_transcript == ""
    assert session._pending_transcript == ""
    assert len(loop.scheduled) == timers, "a duplicate must not arm endpointing"
    assert processed == [DISPATCHED]

    await _release(session, hold)
    _fire_live(loop)
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    assert processed == [DISPATCHED], "a duplicate must never become a second turn"


@pytest.mark.asyncio
async def test_prefix_tail_of_the_dispatched_utterance_is_still_rejected():
    """Preservation guard: a late partial of what was already answered."""
    hold = asyncio.Event()
    session, loop, _processed = make_session(hold=hold)
    await _dispatch_turn(session, loop)

    await session._set_transcript_interim("i would like a room")

    assert session._pending_transcript == ""
    assert session._latest_interim == ""

    await _release(session, hold)


@pytest.mark.asyncio
async def test_case_and_whitespace_normalised_duplicate_is_still_rejected():
    """Preservation guard: normalisation is whitespace + case, nothing fuzzier."""
    hold = asyncio.Event()
    session, loop, _processed = make_session(hold=hold)
    await _dispatch_turn(session, loop)

    await session._accumulate_transcript("  I Would   Like A ROOM for two nights ")

    assert session._committed_transcript == ""
    assert session._pending_transcript == ""

    await _release(session, hold)


@pytest.mark.asyncio
async def test_superset_adding_only_punctuation_is_still_rejected():
    """Preservation guard: a tail that adds a full stop adds nothing."""
    hold = asyncio.Event()
    session, loop, _processed = make_session(hold=hold)
    await _dispatch_turn(session, loop)

    await session._accumulate_transcript(DISPATCHED + " .")

    assert session._committed_transcript == ""
    assert session._pending_transcript == ""

    await _release(session, hold)


@pytest.mark.asyncio
async def test_blank_result_during_an_active_turn_is_still_rejected():
    """Preservation guard: nothing material to lose, so nothing to queue."""
    hold = asyncio.Event()
    session, loop, _processed = make_session(hold=hold)
    await _dispatch_turn(session, loop)

    timers = len(loop.scheduled)
    await session._accumulate_transcript("   ")
    await session._set_transcript_interim("")

    assert session._committed_transcript == ""
    assert session._pending_transcript == ""
    assert len(loop.scheduled) == timers

    await _release(session, hold)


@pytest.mark.asyncio
async def test_rejected_tail_still_leaves_the_reprompt_timer_alone():
    """Preservation guard: the rejection path mutates no timer and no counter."""
    hold = asyncio.Event()
    session, loop, _processed = make_session(hold=hold)
    await _dispatch_turn(session, loop)

    delivered = await session._send_tts_done(sentence="One moment.")
    assert delivered is True
    armed = session._reprompt_task
    assert armed is not None
    session._reprompt_count = 1

    await session._accumulate_transcript(DISPATCHED)
    await session._set_transcript_interim("i would like a room")

    assert session._reprompt_task is armed, "a rejected tail must not re-arm the nudge"
    assert not armed.cancelled(), "a rejected tail must not cancel the nudge"
    assert session._reprompt_count == 1

    session._cancel_reprompt()
    await _release(session, hold)


@pytest.mark.asyncio
async def test_rejected_tail_still_emits_bounded_privacy_safe_telemetry(caplog):
    """Preservation guard: reject-before-counters ordering and the closed event."""
    hold = asyncio.Event()
    session, loop, _processed = make_session(hold=hold)
    await _dispatch_turn(session, loop)

    with caplog.at_level("INFO"):
        await session._accumulate_transcript(DISPATCHED)
        await session._set_transcript_interim("i would like a room")

    records = _post_dispatch_records(caplog)
    assert len(records) == 2, records
    for line in records:
        match = re.match(
            r"^smartpbx_media event=stt_post_dispatch_result "
            r"result_type=(final|interim) action=ignored_active_turn "
            r"elapsed_ms=\d+$",
            line,
        )
        assert match, line
        for fragment in ("room", "would", "nights"):
            assert fragment not in line
    assert session._smartpbx_stt_final_events == 0, (
        "a refused result must be refused BEFORE the per-turn counters"
    )
    assert session._smartpbx_stt_interim_events == 0

    await _release(session, hold)


@pytest.mark.asyncio
async def test_twilio_media_streams_session_rejects_a_stale_tail_without_smartpbx_logs(
    caplog,
):
    """Preservation guard for the SHARED policy on the Twilio transport."""
    hold = asyncio.Event()
    session, loop, processed = make_session(hold=hold, smartpbx=False, lang="si")
    await _dispatch_turn(session, loop, "mata kamarayak one")

    timers = len(loop.scheduled)
    with caplog.at_level("INFO"):
        await session._accumulate_transcript("mata kamarayak one")

    assert session._committed_transcript == ""
    assert session._pending_transcript == ""
    assert len(loop.scheduled) == timers
    assert processed == ["mata kamarayak one"]
    assert _post_dispatch_records(caplog) == []
    assert "smartpbx_media" not in caplog.text

    await _release(session, hold)
