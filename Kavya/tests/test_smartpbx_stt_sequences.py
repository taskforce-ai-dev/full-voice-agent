"""Deterministic STT event-sequence coverage (Phase C, reliability plan §6).

Drives the shared STT boundary (`_on_stt_result` / `_on_stt_interim` /
`_flush_transcript`) directly with controlled fake clocks and, for the
callback-threaded paths, the real (but sleep-free) asyncio loop — exactly the
patterns already established in `test_stt_endpointing.py`,
`test_bargein_threshold.py` and `test_capture_dictation_buffering.py`. No
wall-clock sleeps: every timer is a `FakeLoop.call_later` handle fired
manually, and every `run_coroutine_threadsafe` hop is drained with
`await asyncio.sleep(0)`.

This phase changes NO runtime behavior. It keeps the current STT provider,
current endpointing timings, and does not add turn-per-interim, fuzzy interim
dedup, or Google-cumulative-interim assumptions to Azure. It covers exactly
the nine-item matrix from §6:
  1. repeated interim -> final
  2. interim-only -> timeout
  3. final replacing interim
  4. late final after timeout
  5. short noise
  6. echo while Kavya speaks
  7. genuine sustained interruption
  8. multi-clause speech with a natural pause
  9. fragmented spoken-name/number capture
"""

from __future__ import annotations

import asyncio

import pytest

import server


# ---------------------------------------------------------------------------
# Fixtures (same shape as test_stt_endpointing.py / test_capture_dictation_buffering.py)
# ---------------------------------------------------------------------------


class _Scheduled:
    def __init__(self, delay, callback):
        self.delay = delay
        self.callback = callback
        self.cancelled = False

    def cancel(self):
        self.cancelled = True


class FakeLoop:
    """Captures call_later so timer arming/cancellation is asserted deterministically."""

    def __init__(self):
        self.scheduled: list[_Scheduled] = []

    def call_later(self, delay, callback):
        handle = _Scheduled(delay, callback)
        self.scheduled.append(handle)
        return handle

    @property
    def last(self) -> _Scheduled:
        return self.scheduled[-1]


async def _noop(*_args, **_kwargs):
    return None


def make_session(*, media_transport_enabled=True, lang="en"):
    """A direct-SmartPBX-English session whose LLM turn is recorded, not run.

    `_media_transport` is a plain sentinel object (never `.clear_audio()`d in
    these tests) purely so `_is_direct_smartpbx_english()` is True and turn
    telemetry (interim/final counters, endpoint_source) is exercised, matching
    `test_smartpbx_flush_starts_a_turn_with_the_endpoint_source` in
    test_stt_endpointing.py.
    """
    session = server.MediaStreamSession(websocket=None, lang=lang, media_transport=None)
    session._smartpbx_transfer_context = object()
    if media_transport_enabled:
        session._media_transport = object()
    loop = FakeLoop()
    session._event_loop = loop
    processed: list[str] = []

    async def record(text):
        processed.append(text)

    session._process_utterance = record
    return session, loop, processed


def make_live_session(monkeypatch):
    """A session driven through the real STT-thread callbacks on a real loop.

    Used only for the echo/barge-in scenarios (6, 7), which hop through
    `asyncio.run_coroutine_threadsafe` and so need a real running loop rather
    than FakeLoop. `_clear_media_audio` is stubbed (as in
    test_bargein_threshold.py's make_session) because there is no real
    transport/websocket to clear against here.
    """
    session = server.MediaStreamSession(websocket=None, lang="en", media_transport=None)
    session._event_loop = asyncio.get_running_loop()
    session._smartpbx_transfer_context = object()
    session._media_transport = object()
    monkeypatch.setattr(session, "_clear_media_audio", _noop)
    return session


def _seed_assistant_speech(session, sentence: str) -> None:
    session._start_assistant_turn_delivery_tracking()
    session._record_generated_sentence(sentence)
    generation = session._assistant_turn_generation
    session._record_delivered_sentence(sentence, generation)


# ---------------------------------------------------------------------------
# 1. repeated interim -> final
# ---------------------------------------------------------------------------


def test_repeated_interim_then_final_dispatches_once_with_correct_counters():
    session, loop, processed = make_session()

    async def scenario():
        await session._set_transcript_interim("room")
        await session._set_transcript_interim("room for")
        await session._set_transcript_interim("room for two")
        await session._accumulate_transcript("room for two guests please")
        loop.last.callback()
        await asyncio.sleep(0)
        await asyncio.sleep(0)

    asyncio.run(scenario())

    assert processed == ["room for two guests please"], "the final text must win outright"
    turn_id = session._active_smartpbx_turn_id
    assert turn_id is not None
    state = session._turn_telemetry._turns[turn_id]
    assert state.stt_interim_events == 3
    assert state.stt_final_events == 1
    assert state.endpoint_source == "final"


# ---------------------------------------------------------------------------
# 2. interim-only -> timeout
# ---------------------------------------------------------------------------


def test_interim_only_dispatches_once_on_timeout_and_a_stale_refire_is_a_noop():
    session, loop, processed = make_session()

    async def scenario():
        await session._set_transcript_interim("checking availability for next week")
        assert loop.last.delay == server.ENDPOINTING_SILENCE
        loop.last.callback()
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        # A stale/duplicate timer firing again after dispatch (e.g. a second
        # scheduled callback racing the first) must not start a second turn.
        await session._flush_transcript()

    asyncio.run(scenario())

    assert processed == ["checking availability for next week"]
    turn_id = session._active_smartpbx_turn_id
    state = session._turn_telemetry._turns[turn_id]
    assert state.endpoint_source == "interim"


# ---------------------------------------------------------------------------
# 3. final replacing interim
# ---------------------------------------------------------------------------


def test_final_replaces_pending_interim_no_double_dispatch():
    session, loop, processed = make_session()

    async def scenario():
        await session._set_transcript_interim("two five")
        interim_timer = loop.last
        assert interim_timer.delay == server.ENDPOINTING_SILENCE

        await session._accumulate_transcript("two five zero")
        assert interim_timer.cancelled, "the final must cancel the stale interim timer"
        assert loop.last is not interim_timer
        assert loop.last.delay == server.STT_FINAL_GRACE_SECONDS

        loop.last.callback()
        await asyncio.sleep(0)
        await asyncio.sleep(0)

    asyncio.run(scenario())

    assert processed == ["two five zero"], (
        "the final supersedes the interim outright; it must not be concatenated"
    )


# ---------------------------------------------------------------------------
# 4. late final after timeout
# ---------------------------------------------------------------------------


def test_late_final_after_dispatch_does_not_redispatch_or_mutate_history():
    session, loop, processed = make_session()

    async def slow_process(text):
        processed.append(text)
        # A late final for the segment that was JUST dispatched arrives while
        # this turn is still in flight (`_utterance_dispatched` guard is up).
        await session._accumulate_transcript("late fragment nobody asked for")
        await session._flush_transcript()

    session._process_utterance = slow_process

    async def scenario():
        await session._accumulate_transcript("original utterance")
        loop.last.callback()
        await asyncio.sleep(0)
        await asyncio.sleep(0)

    asyncio.run(scenario())

    assert processed == ["original utterance"], "the late final must not start a second dispatch"
    assert session.full_transcript == [{"role": "user", "text": "original utterance"}], (
        "no stale/duplicate history mutation from the late final"
    )


# ---------------------------------------------------------------------------
# 5. short noise
# ---------------------------------------------------------------------------


def test_whitespace_only_fragment_does_not_create_a_turn():
    session, loop, processed = make_session()

    async def scenario():
        await session._set_transcript_interim("   ")
        loop.last.callback()
        await asyncio.sleep(0)
        await asyncio.sleep(0)

    asyncio.run(scenario())

    assert processed == []
    assert session._active_smartpbx_turn_id is None, "no telemetry turn may start for empty noise"


def test_backchannel_classifier_flags_thinking_noise_but_is_not_wired_into_dispatch():
    """`_is_backchannel`/`BACKCHANNEL_TOKENS` (server.py) classify short
    thinking-noise fragments ("uh", "hmm") correctly on their own, but nothing
    on the STT dispatch path (`_on_stt_result`/`_on_stt_interim`/
    `_flush_transcript`) calls `_is_backchannel` today — confirmed by
    `rg -n "_is_backchannel" Kavya/server.py` returning only the definition.
    A standalone "uh" therefore currently DOES reach `_flush_transcript` as
    real content and dispatches a turn. Phase C is tests-only and must not
    wire this in (that would be an endpointing/dispatch behavior change
    outside the approved scope) — this pins the classifier's own correctness
    and documents the gap for a future evidence-backed patch.
    """
    assert server._is_backchannel("uh") is True
    assert server._is_backchannel("hmm") is True
    assert server._is_backchannel("uh uh") is True
    assert server._is_backchannel("I'd like a room") is False
    assert server._is_backchannel("071") is False, "digit-bearing text is never backchannel"


# ---------------------------------------------------------------------------
# 6. echo while Kavya speaks
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_echo_final_rejected_before_bargein_endpointing_transcript_and_history(monkeypatch):
    session = make_live_session(monkeypatch)
    accumulated: list[str] = []

    async def fake_accumulate(text):
        accumulated.append(text)

    monkeypatch.setattr(session, "_accumulate_transcript", fake_accumulate)

    sentence = "yes, we can help with room bookings and half board options"
    _seed_assistant_speech(session, sentence)
    session._is_speaking = True
    session._speaking_since = server.time.monotonic() - 2.0  # outside debounce
    speak_generation_before = session._speak_generation
    rejected: list[tuple[int, float]] = []
    session._record_echo_rejection = lambda chars, score: rejected.append((chars, score))

    transcript = "yes we can help with room bookings and half board options"
    session._on_stt_result(transcript)
    await asyncio.sleep(0)
    await asyncio.sleep(0)

    # No barge-in.
    assert session._is_speaking is True
    assert session._speak_generation == speak_generation_before
    # No endpointing/transcript mutation (the fake would have recorded it).
    assert accumulated == []
    assert session._pending_transcript == ""
    assert session._committed_transcript == ""
    # No history mutation.
    assert session.full_transcript == []
    # The echo gate ran and was counted.
    assert rejected and rejected[0][0] == len(transcript)


@pytest.mark.asyncio
async def test_echo_interim_rejected_before_bargein_endpointing_transcript_and_history(monkeypatch):
    session = make_live_session(monkeypatch)
    interims: list[str] = []

    async def fake_interim(text):
        interims.append(text)

    monkeypatch.setattr(session, "_set_transcript_interim", fake_interim)

    sentence = "if you prefer, we can also include full board"
    _seed_assistant_speech(session, sentence)
    session._is_speaking = True
    session._speaking_since = server.time.monotonic() - 2.0
    speak_generation_before = session._speak_generation
    rejected: list[tuple[int, float]] = []
    session._record_echo_rejection = lambda chars, score: rejected.append((chars, score))

    transcript = "if you prefer we can also include full board"
    session._on_stt_interim(transcript)
    await asyncio.sleep(0)
    await asyncio.sleep(0)

    assert session._is_speaking is True
    assert session._speak_generation == speak_generation_before
    assert interims == []
    assert session._pending_transcript == ""
    assert session.full_transcript == []
    assert rejected and rejected[0][0] == len(transcript)


# ---------------------------------------------------------------------------
# 7. genuine sustained interruption
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_genuine_interruption_barges_in_and_keeps_prior_transcript(monkeypatch):
    session = make_live_session(monkeypatch)
    session.full_transcript.append({"role": "user", "text": "prior turn kept intact"})
    session._is_speaking = True
    session._speaking_since = server.time.monotonic() - 2.0  # outside debounce
    speak_generation_before = session._speak_generation
    # Stale buffered text from the turn being interrupted.
    session._pending_transcript = "stale text from interrupted turn"
    session._committed_transcript = "stale text from interrupted turn"

    transcript = "no wait I want to change my dates please"
    session._on_stt_result(transcript)
    await asyncio.sleep(0)
    await asyncio.sleep(0)

    assert session._is_speaking is False, "queued speech must stop"
    assert session._speak_generation == speak_generation_before + 1, (
        "a new speech generation fences off the interrupted turn's queued audio"
    )
    assert session._pending_transcript == ""
    assert session._committed_transcript == ""
    # The call's transcript log survives the interruption; only the live
    # buffer for the turn being interrupted is cleared.
    assert session.full_transcript == [{"role": "user", "text": "prior turn kept intact"}]


# ---------------------------------------------------------------------------
# 8. multi-clause speech with a natural pause
# ---------------------------------------------------------------------------


def test_multi_clause_pause_below_endpointing_threshold_does_not_split_the_utterance():
    session, loop, processed = make_session()

    async def scenario():
        await session._accumulate_transcript("I'd like to book a room")
        grace = loop.last
        assert grace.delay == server.STT_FINAL_GRACE_SECONDS

        # A short natural pause, then the second clause arrives as another
        # provider final before the grace timer fires — this must extend the
        # same utterance, not start a new one.
        await session._accumulate_transcript("for the fourteenth of August")
        assert grace.cancelled, "the pause must not have already flushed the first clause"
        assert loop.last is not grace

        loop.last.callback()
        await asyncio.sleep(0)
        await asyncio.sleep(0)

    asyncio.run(scenario())

    assert processed == ["I'd like to book a room for the fourteenth of August"]


# ---------------------------------------------------------------------------
# 9. fragmented spoken-name/number capture
# ---------------------------------------------------------------------------


def test_fragmented_spoken_name_capture_combines_into_one_dispatch():
    """Mirrors test_capture_dictation_buffering.py's digit-fragment combining,
    for a spelled name instead of a phone number."""
    session, loop, processed = make_session()
    session._enter_capture_mode()

    async def scenario():
        for fragment in ("s m", "i t", "h"):
            await session._accumulate_transcript(fragment)
            # Every final refreshes ONE combining window instead of dispatching.
            assert processed == []
            assert loop.last.delay >= server.CAPTURE_ENDPOINTING_SILENCE_SECONDS

        loop.last.callback()
        await asyncio.sleep(0)
        await asyncio.sleep(0)

    asyncio.run(scenario())

    assert processed == ["s m i t h"]
    assert session._pending_transcript == ""
    assert session._committed_transcript == ""


def test_fragmented_spoken_number_capture_combines_into_one_dispatch():
    session, loop, processed = make_session()
    session._enter_capture_mode()

    async def scenario():
        for fragment in ("zero seven seven", "one two three", "four five six eight"):
            await session._accumulate_transcript(fragment)
            assert processed == []
            assert loop.last.delay >= server.CAPTURE_ENDPOINTING_SILENCE_SECONDS

        loop.last.callback()
        await asyncio.sleep(0)
        await asyncio.sleep(0)

    asyncio.run(scenario())

    assert processed == ["zero seven seven one two three four five six eight"]
