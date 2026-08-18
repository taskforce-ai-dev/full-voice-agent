"""STT endpoint ownership: a dispatched turn owns the transcript state.

Production evidence (no packet loss, no stream failure, Google STT active,
endpoint silence 0.85s / final grace 0.45s, finals dispatching ~451ms later):
additional finals AND interims sometimes arrive after `_utterance_dispatched`
is already True. The accumulator accepted those late results — it bumped the
per-turn STT counters, mutated `_pending_transcript` / `_committed_transcript`
and armed a fresh endpointing timer. `_flush_transcript` then returned on its
exactly-once guard WITHOUT clearing those buffers, so the late speech either
contaminated the next turn, became a spurious later turn, or disappeared at
hangup.

Two further defects are covered here:
  * `_latest_interim` was mutated from the synchronous STT worker thread —
    transcript-owned state must only change on the event loop.
  * exact cumulative-interim detection existed but the code still concatenated
    the committed prefix again, duplicating the text.

Scope note (second #269 review round): rejection is turn-scoped and narrow. Only
results carrying NO material characters — empty, whitespace or punctuation only —
are refused, because that is the one thing provable without provider result
identity, and provider result identity never reaches this code (both STT stream
classes hand their callbacks a bare `str`). Those are what this file injects.
ALL caller speech landing in the same window, including a verbatim repeat of the
dispatched utterance, is admitted and queued as the next turn;
`test_stt_post_dispatch_admission.py` and
`test_stt_post_dispatch_material_admission.py` own that half, and
`test_deferred_speech_lifecycle_ownership.py` owns what happens to it at every
lifecycle boundary.

Everything below is deterministic: `Event` barriers and fake timer handles,
never a wall-clock sleep. Tests marked "preservation guard" hold in BOTH the
pre-fix and post-fix trees on purpose — they exist so the rejection gate
cannot be landed by breaking barge-in, capture mode, teardown or Twilio.
"""

from __future__ import annotations

import asyncio
import re
import threading

import pytest

import server


# ---------------------------------------------------------------------------
# Fixtures — same shape as test_utterance_dispatch_once.py / test_stt_endpointing.py
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


async def _noop(*_args, **_kwargs):
    return None


POST_DISPATCH_EVENT = "smartpbx_media event=stt_post_dispatch_result"


def make_session(*, hold=None, smartpbx=True, lang="en"):
    """A direct-SmartPBX-English session whose LLM turn is recorded, not run.

    `hold` (an `asyncio.Event`) blocks inside `_process_utterance`, holding the
    dispatched turn open so late STT results land in the exact window the
    production defect occurs in.
    """
    session = server.MediaStreamSession(websocket=None, lang=lang, media_transport=None)
    if smartpbx:
        session._smartpbx_transfer_context = object()
        session._media_transport = object()
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


async def _dispatch_turn(session, loop, text="original utterance"):
    """Dispatch one turn and leave it in flight (held inside the runner)."""
    await session._accumulate_transcript(text)
    loop.last.callback()
    await asyncio.sleep(0)
    assert session._utterance_dispatched is True, "turn must be in flight"


def _post_dispatch_records(caplog):
    return [
        record.getMessage()
        for record in caplog.records
        if record.getMessage().startswith(POST_DISPATCH_EVENT)
    ]


# ---------------------------------------------------------------------------
# 1. a REFUSED (non-material) final while a turn is in flight
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_refused_final_during_active_turn_mutates_no_transcript_state():
    hold = asyncio.Event()
    session, loop, processed = make_session(hold=hold)
    await _dispatch_turn(session, loop)

    timers = len(loop.scheduled)
    history = list(session.full_transcript)

    # A result with no material characters: nothing here is caller speech.
    # (A repeat of the dispatched text would now be ADMITTED — see
    # test_stt_post_dispatch_material_admission.py.)
    await session._accumulate_transcript("   ")

    assert session._committed_transcript == "", "a refused result must not commit text"
    assert session._pending_transcript == "", "a refused result must not set pending text"
    assert session._latest_interim == ""
    assert session._smartpbx_stt_final_events == 0, (
        "the per-turn final counter belongs to the next turn, not the dispatched one"
    )
    assert len(loop.scheduled) == timers, "a refused result must not arm an endpointing timer"
    assert session._endpointing_handle is None
    assert processed == ["original utterance"], "no redispatch"
    assert session.full_transcript == history, "no history mutation"

    hold.set()
    await asyncio.sleep(0)


@pytest.mark.asyncio
async def test_refused_final_does_not_contaminate_the_next_turn():
    hold = asyncio.Event()
    session, loop, processed = make_session(hold=hold)
    await _dispatch_turn(session, loop)

    await session._accumulate_transcript("   ")

    hold.set()
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    assert session._utterance_dispatched is False, "turn 1 released the guard"

    await session._accumulate_transcript("next turn speech")
    loop.last.callback()
    await asyncio.sleep(0)
    await asyncio.sleep(0)

    assert processed == ["original utterance", "next turn speech"], (
        "the refused result must not be prepended to the next guest turn"
    )
    assert session.full_transcript == [
        {"role": "user", "text": "original utterance"},
        {"role": "user", "text": "next turn speech"},
    ]


@pytest.mark.asyncio
async def test_refused_final_never_becomes_a_spurious_later_turn():
    hold = asyncio.Event()
    session, loop, processed = make_session(hold=hold)
    await _dispatch_turn(session, loop)

    await session._accumulate_transcript("   ")

    hold.set()
    await asyncio.sleep(0)
    await asyncio.sleep(0)

    # Every timer still live fires after the runner unwound. None of them may
    # resurrect the refused result as a turn of its own.
    for handle in loop.live:
        handle.callback()
    await asyncio.sleep(0)
    await asyncio.sleep(0)

    assert processed == ["original utterance"]
    assert session.full_transcript == [{"role": "user", "text": "original utterance"}]


# ---------------------------------------------------------------------------
# 2. REFUSED (non-material) INTERIMS, both timer/runner orderings
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_refused_interim_when_timer_fires_before_runner_finishes():
    hold = asyncio.Event()
    session, loop, processed = make_session(hold=hold)
    await _dispatch_turn(session, loop)

    timers = len(loop.scheduled)
    # A trailing interim carrying no material characters at all.
    await session._set_transcript_interim(" . ")

    assert session._pending_transcript == "", "a refused interim must not set pending text"
    assert session._committed_transcript == ""
    assert session._latest_interim == ""
    assert session._smartpbx_stt_interim_events == 0, (
        "the per-turn interim counter belongs to the next turn"
    )
    assert len(loop.scheduled) == timers, "a refused interim must not arm an endpointing timer"

    # Timers fire while the runner is still held.
    for handle in loop.live:
        handle.callback()
    await asyncio.sleep(0)
    assert processed == ["original utterance"]

    hold.set()
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    assert processed == ["original utterance"]
    assert session.full_transcript == [{"role": "user", "text": "original utterance"}]


@pytest.mark.asyncio
async def test_refused_interim_when_runner_finishes_before_timer():
    hold = asyncio.Event()
    session, loop, processed = make_session(hold=hold)
    await _dispatch_turn(session, loop)

    timers = len(loop.scheduled)
    await session._set_transcript_interim(" . ")
    assert len(loop.scheduled) == timers

    # Runner unwinds first, releasing the guard, and only THEN do timers fire.
    hold.set()
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    assert session._utterance_dispatched is False

    for handle in loop.live:
        handle.callback()
    await asyncio.sleep(0)
    await asyncio.sleep(0)

    assert processed == ["original utterance"], (
        "a released guard must not let a refused result dispatch as its own turn"
    )
    assert session.full_transcript == [{"role": "user", "text": "original utterance"}]
    assert session._pending_transcript == ""
    assert session._committed_transcript == ""


# ---------------------------------------------------------------------------
# 3. thread ownership — the STT worker thread owns no transcript state
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_interim_callback_from_a_real_thread_mutates_nothing_off_loop():
    """`_on_stt_interim` runs on the synchronous STT worker thread.

    The loop-side coroutine is gated behind an `Event` that is still clear when
    the assertion runs, so anything observed then was written by the worker
    thread itself.
    """
    session = server.MediaStreamSession(websocket=None, lang="en", media_transport=None)
    session._smartpbx_transfer_context = object()
    session._media_transport = object()
    session._event_loop = asyncio.get_running_loop()
    session._clear_media_audio = _noop

    released = asyncio.Event()
    idents: list[int] = []
    real = session._set_transcript_interim

    async def gated(text):
        idents.append(threading.get_ident())
        await released.wait()
        await real(text)

    session._set_transcript_interim = gated
    loop_ident = threading.get_ident()

    await asyncio.to_thread(session._on_stt_interim, "cumulative interim words")

    assert session._latest_interim == "", (
        "the STT worker thread must not write _latest_interim"
    )
    assert session._pending_transcript == ""
    assert session._endpointing_handle is None

    released.set()
    await asyncio.sleep(0)
    await asyncio.sleep(0)

    assert idents == [loop_ident], "transcript state must change on the event loop only"
    assert session._latest_interim == "cumulative interim words"
    if session._endpointing_handle is not None:
        session._endpointing_handle.cancel()
        session._endpointing_handle = None


@pytest.mark.asyncio
async def test_final_callback_from_a_real_thread_does_not_clear_interim_off_loop():
    session = server.MediaStreamSession(websocket=None, lang="en", media_transport=None)
    session._smartpbx_transfer_context = object()
    session._media_transport = object()
    session._event_loop = asyncio.get_running_loop()
    session._clear_media_audio = _noop
    session._latest_interim = "seeded on the loop"

    released = asyncio.Event()
    idents: list[int] = []
    real = session._accumulate_transcript

    async def gated(text):
        idents.append(threading.get_ident())
        await released.wait()
        await real(text)

    session._accumulate_transcript = gated
    loop_ident = threading.get_ident()

    await asyncio.to_thread(session._on_stt_result, "a complete final utterance")

    assert session._latest_interim == "seeded on the loop", (
        "the STT worker thread must not clear _latest_interim"
    )

    released.set()
    await asyncio.sleep(0)
    await asyncio.sleep(0)

    assert idents == [loop_ident]
    assert session._latest_interim == "", "the loop-side final supersedes the interim"
    if session._endpointing_handle is not None:
        session._endpointing_handle.cancel()
        session._endpointing_handle = None


# ---------------------------------------------------------------------------
# 4. genuine barge-in still wins (preservation guard — holds pre- and post-fix)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_genuine_barge_in_while_speaking_runs_before_the_rejection_gate(caplog):
    """Substantive speech over Kavya must barge in, not be swallowed as late.

    The speaking-time branch of `_on_stt_result` is reached BEFORE anything can
    hand the result to the accumulator, so the rejection gate can never shadow
    a real interruption. Preservation guard.
    """
    hold = asyncio.Event()
    session = server.MediaStreamSession(websocket=None, lang="en", media_transport=None)
    session._smartpbx_transfer_context = object()
    session._media_transport = object()
    session._event_loop = asyncio.get_running_loop()
    session._clear_media_audio = _noop
    processed: list[str] = []

    async def record(text):
        processed.append(text)
        await hold.wait()

    session._process_utterance = record

    await session._accumulate_transcript("original utterance")
    if session._endpointing_handle is not None:
        session._endpointing_handle.cancel()
        session._endpointing_handle = None
    flush = asyncio.ensure_future(session._flush_transcript())
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    assert session._utterance_dispatched is True
    assert processed == ["original utterance"]

    generation = session._speak_generation
    session._is_speaking = True

    with caplog.at_level("INFO"):
        session._on_stt_result("actually i would like to change that booking")
        await asyncio.sleep(0)
        await asyncio.sleep(0)

    assert session._is_speaking is False, "genuine barge-in must stop the speech"
    assert session._speak_generation == generation + 1, "the speak fence must re-anchor"
    assert session._smartpbx_barge_ins == 1
    assert session._utterance_dispatched is False, "barge-in releases the guard"
    assert _post_dispatch_records(caplog) == [], (
        "a barge-in is not a late result and must not be recorded as ignored"
    )

    hold.set()
    await flush
    if session._endpointing_handle is not None:
        session._endpointing_handle.cancel()
        session._endpointing_handle = None


@pytest.mark.asyncio
async def test_speech_after_a_barge_in_dispatches_a_clean_turn():
    """Preservation guard: the barge-in utterance's successor is not polluted."""
    hold = asyncio.Event()
    session, loop, processed = make_session(hold=hold)
    await _dispatch_turn(session, loop)

    session._is_speaking = True
    await session._handle_bargein()
    session._is_speaking = False

    await session._accumulate_transcript("actually never mind")
    loop.last.callback()
    await asyncio.sleep(0)
    await asyncio.sleep(0)

    assert processed == ["original utterance", "actually never mind"]
    hold.set()
    await asyncio.sleep(0)


# ---------------------------------------------------------------------------
# 5. capture mode, teardown, Twilio (preservation guards)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_capture_fragments_before_dispatch_still_combine_into_one_utterance():
    """Preservation guard: the fragment-combining contract is untouched."""
    session, loop, processed = make_session()
    session._enter_capture_mode(reason="test")
    assert session._is_capture_mode_active() is True

    for fragment in ("zero seven seven", "one two three", "four five six"):
        await session._accumulate_transcript(fragment)
        assert loop.last.delay == session._capture_turn_timeout(final=True)

    loop.last.callback()
    await asyncio.sleep(0)
    await asyncio.sleep(0)

    assert processed == ["zero seven seven one two three four five six"]


@pytest.mark.asyncio
async def test_capture_fragment_after_dispatch_is_queued_not_merged_or_dropped():
    """The rest of a dictated number is speech, not a tail of the dispatch.

    It must not join the turn already running (that turn was answered for the
    digits it carried), and it must not vanish: it is buffered and dispatched as
    the next turn once the guard releases.
    """
    hold = asyncio.Event()
    session, loop, processed = make_session(hold=hold)
    session._enter_capture_mode(reason="test")

    await session._accumulate_transcript("zero seven seven one two three")
    loop.last.callback()
    await asyncio.sleep(0)
    assert processed == ["zero seven seven one two three"]

    timers = len(loop.scheduled)
    await session._accumulate_transcript("four five six")

    assert session._committed_transcript == "four five six", (
        "the remaining digits must survive the active turn"
    )
    assert session._pending_transcript == "four five six"
    assert len(loop.scheduled) == timers + 1, "admitted digits must arm endpointing"
    assert processed == ["zero seven seven one two three"], "no merge into the old turn"

    # The capture deadline expires inside the turn, then the turn releases.
    loop.last.callback()
    await asyncio.sleep(0)
    hold.set()
    for _ in range(20):
        await asyncio.sleep(0)
        if session._utterance_dispatched is False:
            break
    for handle in loop.live:
        handle.callback()
    await asyncio.sleep(0)
    await asyncio.sleep(0)

    assert processed == ["zero seven seven one two three", "four five six"]
    assert session.full_transcript == [
        {"role": "user", "text": "zero seven seven one two three"},
        {"role": "user", "text": "four five six"},
    ]


@pytest.mark.asyncio
async def test_torn_down_session_drops_late_results_without_telemetry(caplog):
    """Preservation guard: the teardown guard stays ahead of the new gate."""
    session, loop, processed = make_session()
    session._finalize_smartpbx_turns()
    assert session._smartpbx_torn_down is True

    with caplog.at_level("INFO"):
        await session._accumulate_transcript("post teardown final")
        await session._set_transcript_interim("post teardown interim")
        await session._flush_transcript()

    assert processed == []
    assert session._pending_transcript == ""
    assert session._committed_transcript == ""
    assert session._latest_interim == ""
    assert session._endpointing_handle is None
    assert session._utterance_dispatched is False
    assert loop.scheduled == [], "no timer may be armed after teardown"
    assert _post_dispatch_records(caplog) == [], (
        "a torn-down session is not an active turn — nothing to record"
    )


@pytest.mark.asyncio
async def test_twilio_language_session_refuses_empty_results_without_smartpbx_logs(caplog):
    """Preservation guard: a Twilio Media Streams call emits no SmartPBX events.

    The post-dispatch policy is shared with this transport by design; only the
    SmartPBX log vocabulary is not. Retargeted to what is still refused —
    admission of a repeat on this same transport is pinned in
    `test_stt_post_dispatch_material_admission.py`.
    """
    hold = asyncio.Event()
    session, loop, processed = make_session(hold=hold, smartpbx=False, lang="si")
    assert session._is_smartpbx_session() is False

    await session._accumulate_transcript("mata kamarayak one")
    loop.last.callback()
    await asyncio.sleep(0)
    assert processed == ["mata kamarayak one"]

    timers = len(loop.scheduled)
    with caplog.at_level("INFO"):
        await session._accumulate_transcript("  ")
        await session._set_transcript_interim(" . ")

    assert session._committed_transcript == ""
    assert session._pending_transcript == ""
    assert len(loop.scheduled) == timers
    assert _post_dispatch_records(caplog) == []
    assert "smartpbx_media" not in caplog.text

    hold.set()
    await asyncio.sleep(0)


# ---------------------------------------------------------------------------
# 6. exact cumulative interim — the committed prefix appears once
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_exact_cumulative_interim_does_not_duplicate_the_committed_prefix():
    session, loop, processed = make_session()

    await session._accumulate_transcript("first segment")
    await session._set_transcript_interim("first segment second segment")

    pending = session._pending_transcript
    assert pending == "first segment second segment"
    assert pending.count("first segment") == 1, (
        f"the committed prefix was concatenated twice: {pending!r}"
    )

    loop.last.callback()
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    assert processed == ["first segment second segment"]


@pytest.mark.asyncio
async def test_exact_cumulative_interim_dedup_needs_a_single_separator():
    """A near-miss shape (case-folded prefix) must NOT be treated as cumulative.

    No fuzzy matching: only a byte-exact prefix plus exactly one space is
    de-duplicated. Anything else keeps the conservative concatenation.
    """
    session, _loop, _processed = make_session()

    await session._accumulate_transcript("First final")
    await session._set_transcript_interim("first final second segment")

    assert session._pending_transcript == "First final first final second segment", (
        "an inexact prefix must keep the conservative concatenation"
    )


# ---------------------------------------------------------------------------
# 7. segment-only interims still append (preservation guard)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_segment_only_interim_still_appends_to_the_committed_finals():
    session, loop, processed = make_session()

    await session._accumulate_transcript("first segment")
    await session._set_transcript_interim("second segment")

    assert session._pending_transcript == "first segment second segment"

    loop.last.callback()
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    assert processed == ["first segment second segment"]


@pytest.mark.asyncio
async def test_interim_only_utterance_is_unchanged_without_committed_finals():
    session, loop, processed = make_session()

    await session._set_transcript_interim("i would like a room")
    await session._set_transcript_interim("i would like a room for two")

    assert session._pending_transcript == "i would like a room for two"
    loop.last.callback()
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    assert processed == ["i would like a room for two"]


# ---------------------------------------------------------------------------
# Telemetry — closed enums, one clamped integer, nothing else
# ---------------------------------------------------------------------------


_POST_DISPATCH_RE = re.compile(
    r"^smartpbx_media event=stt_post_dispatch_result "
    r"result_type=(?P<result_type>final|interim) "
    r"action=(?P<action>ignored_active_turn) "
    r"elapsed_ms=(?P<elapsed_ms>\d+)$"
)


@pytest.mark.asyncio
async def test_post_dispatch_telemetry_records_both_result_types_once_each(caplog):
    hold = asyncio.Event()
    session, loop, _processed = make_session(hold=hold)
    await _dispatch_turn(session, loop)

    with caplog.at_level("INFO"):
        await session._accumulate_transcript("   ")
        await session._set_transcript_interim("...")

    records = _post_dispatch_records(caplog)
    assert len(records) == 2, records
    parsed = [_POST_DISPATCH_RE.match(line) for line in records]
    assert all(parsed), records
    assert [m.group("result_type") for m in parsed] == ["final", "interim"]
    assert all(m.group("action") == "ignored_active_turn" for m in parsed)

    hold.set()
    await asyncio.sleep(0)


@pytest.mark.asyncio
async def test_post_dispatch_telemetry_carries_no_transcript_text_or_digits(caplog):
    """Adversarial: the caller's words and phone digits must not reach the log."""
    hold = asyncio.Event()
    session, loop, _processed = make_session(hold=hold)
    secret = "my number is 0771754668 and my name is Priyanka"
    # The OWNING TURN holds the most sensitive text a call ever carries, and the
    # emitter reads that turn's state. Nothing of it may reach the line.
    await _dispatch_turn(session, loop, text=secret)

    with caplog.at_level("INFO"):
        await session._accumulate_transcript("   ")

    line = _post_dispatch_records(caplog)[0]
    match = _POST_DISPATCH_RE.match(line)
    assert match, line
    # The only digits permitted anywhere in the line are the clamped integer.
    assert re.sub(r"elapsed_ms=\d+", "elapsed_ms=", line) == (
        "smartpbx_media event=stt_post_dispatch_result "
        "result_type=final action=ignored_active_turn elapsed_ms="
    )
    for fragment in ("0771754668", "Priyanka", "my number", "name"):
        assert fragment not in line
    # Exact key set — no field may be added without updating the runbook.
    keys = [part.split("=", 1)[0] for part in line.split(" ")[1:]]
    assert keys == ["event", "result_type", "action", "elapsed_ms"]

    hold.set()
    await asyncio.sleep(0)


@pytest.mark.asyncio
async def test_post_dispatch_elapsed_ms_is_clamped_at_both_ends(caplog):
    hold = asyncio.Event()
    session, loop, _processed = make_session(hold=hold)
    await _dispatch_turn(session, loop)

    # A clock that appears to run backwards must not emit a negative integer.
    session._utterance_dispatched_at = None
    with caplog.at_level("INFO"):
        await session._accumulate_transcript("   ")
    assert "elapsed_ms=0" in _post_dispatch_records(caplog)[0]

    caplog.clear()
    # Age no longer gates refusal at all, so no window has to be widened here:
    # what is under test is the clamp on the emitted integer.
    session._utterance_dispatched_at = -10_000_000.0
    with caplog.at_level("INFO"):
        await session._set_transcript_interim("...")
    line = _post_dispatch_records(caplog)[0]
    elapsed = int(_POST_DISPATCH_RE.match(line).group("elapsed_ms"))
    assert elapsed == server.POST_DISPATCH_ELAPSED_MS_MAX
    assert 0 <= elapsed <= 60_000

    hold.set()
    await asyncio.sleep(0)


@pytest.mark.asyncio
async def test_post_dispatch_telemetry_rejects_an_unknown_result_type(caplog):
    """result_type is a closed enum: an unexpected value emits nothing."""
    session, _loop, _processed = make_session()

    with caplog.at_level("INFO"):
        session._emit_post_dispatch_result("transcript")

    assert _post_dispatch_records(caplog) == []
