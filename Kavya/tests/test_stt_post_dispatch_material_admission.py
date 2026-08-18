"""Text plus elapsed time cannot prove provider ownership — so it must not discard.

`test_stt_post_dispatch_admission.py` pinned a predicate that refused a late STT
result when its normalised text was the dispatched utterance, a token-boundary
prefix of it, or a punctuation-only superset of it, AND it arrived inside a two
second window. Both halves of that "proof" are inferences about a provider the
pipeline cannot see:

  * Google's `on_final_result` / `on_interim_result` and Azure's `recognized` /
    `recognizing` callbacks hand this session a bare `str`. No result id, no
    segment id, no audio-time span, no stream epoch reaches `_accumulate_transcript`
    (`GoogleSTTStream._stream_epoch` is an internal gRPC-swap fence and is the
    SAME for a provider tail and for an immediate caller repeat, so plumbing it
    through would settle nothing).
  * A caller who repeats themselves immediately — the single most common thing a
    caller does when an agent goes quiet mid-turn — produces exactly the text and
    exactly the timing the old predicate called a tail.

So the rejection predicate is narrowed to the one thing provable WITHOUT provider
identity: a result carrying no material characters at all (empty, whitespace, or
punctuation only) contains no caller speech, so refusing it cannot lose any.
EVERY material result is admitted through the existing admission path — buffered,
cancelling and resetting the silence re-prompt, dispatched as the NEXT turn once
the active turn releases the guard — including an exact repeat of the dispatched
text, a prefix of it, and a punctuation variation of it.

Every test here is a production shape: guests re-say dates, restart and shorten
a date range, re-read a digit group, and are transcribed with different final
punctuation. Under the old predicate each of those was silently deleted.

Determinism: `Event` barriers hold the turn open, `FakeLoop` captures endpointing
timers instead of awaiting them, and the boundary trio pins `elapsed_ms` directly
because a wall clock cannot be asked to land on an exact millisecond.
"""

from __future__ import annotations

import asyncio

import pytest

import server


# ---------------------------------------------------------------------------
# Fixtures — same shape as test_stt_post_dispatch_admission.py
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
        return [handle for handle in self.scheduled if not handle.cancelled]


class _FakeTransport:
    """Minimal media transport: enough for the real `_send_tts_done` to run."""

    def __init__(self):
        self.marks: list[str] = []

    async def send_mark(self, name: str) -> None:
        self.marks.append(name)


async def _noop(*_args, **_kwargs):
    return None


POST_DISPATCH_EVENT = "smartpbx_media event=stt_post_dispatch_result"
DATES = "the fifth of september to the ninth of september"
DIGITS = "zero seven seven one two three"


def make_session(*, hold=None, smartpbx=True, lang="en"):
    """A direct-SmartPBX-English session whose LLM turn is recorded, not run."""
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


async def _dispatch_turn(session, loop, text=DATES):
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


# ---------------------------------------------------------------------------
# 1. material results the old predicate deleted — every one is caller speech
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_immediate_exact_date_repetition_is_admitted():
    """"The fifth of September to the ninth of September." … "…the ninth of September."

    A guest who hears nothing back repeats the dates verbatim within a second.
    Nothing in the pipeline can distinguish that from a provider re-send, so the
    words must be kept.
    """
    hold = asyncio.Event()
    session, loop, processed = make_session(hold=hold)
    await _dispatch_turn(session, loop)

    timers = len(loop.scheduled)
    await session._accumulate_transcript(DATES)

    assert session._committed_transcript == DATES, (
        "an immediate exact repetition is caller speech and must not be discarded"
    )
    assert session._pending_transcript == DATES
    assert len(loop.scheduled) == timers + 1, (
        "admitted speech must arm endpointing so it can become a turn"
    )
    assert processed == [DATES], "the running turn must not be re-entered"

    await _release(session, hold)


@pytest.mark.asyncio
async def test_date_prefix_correction_is_admitted():
    """The guest restarts and shortens: a result that IS a prefix of the dispatch.

    Dispatched: "the fifth of September to the ninth of September".
    Late result: "the fifth of September" — the guest starting the range again.
    A token-boundary prefix was the old predicate's clearest "tail", and it is
    indistinguishable from this correction.
    """
    hold = asyncio.Event()
    session, loop, processed = make_session(hold=hold)
    await _dispatch_turn(session, loop)

    correction = "the fifth of september"
    await session._accumulate_transcript(correction)

    assert session._committed_transcript == correction, (
        "a prefix of the dispatched text may be the guest restarting the range"
    )
    assert session._pending_transcript == correction
    assert processed == [DATES]

    await _release(session, hold)


@pytest.mark.asyncio
async def test_repeated_capture_digit_group_is_admitted():
    """A guest re-reading the same digit group must not lose those digits."""
    hold = asyncio.Event()
    session, loop, processed = make_session(hold=hold)
    session._enter_capture_mode(reason="test")
    await _dispatch_turn(session, loop, DIGITS)

    await session._accumulate_transcript(DIGITS)

    assert session._committed_transcript == DIGITS, (
        "a repeated digit group is a re-read, not a provider duplicate"
    )
    assert processed == [DIGITS]

    await _release(session, hold)


@pytest.mark.asyncio
async def test_punctuation_variation_of_the_dispatched_text_is_admitted():
    """Same material characters, different punctuation — still caller speech.

    The recognizer's final for a repeated utterance routinely differs from the
    interim-driven dispatch only by a trailing stop. That difference proves
    nothing about who produced it.
    """
    hold = asyncio.Event()
    session, loop, processed = make_session(hold=hold)
    await _dispatch_turn(session, loop)

    variation = DATES + "."
    await session._accumulate_transcript(variation)

    assert session._committed_transcript == variation, (
        "a punctuation variation of the dispatched text must not be discarded"
    )
    assert processed == [DATES]

    await _release(session, hold)


@pytest.mark.asyncio
async def test_admitted_exact_repeat_cancels_and_resets_the_silence_reprompt():
    """Admitted speech takes the whole admission path, re-prompt included."""
    hold = asyncio.Event()
    session, loop, _processed = make_session(hold=hold)
    await _dispatch_turn(session, loop)

    delivered = await session._send_tts_done(sentence="One moment.")
    assert delivered is True
    armed = session._reprompt_task
    assert armed is not None, "a delivered sentence must arm the nudge"
    session._reprompt_count = 1

    await session._accumulate_transcript(DATES)
    await asyncio.sleep(0)

    assert session._reprompt_task is None, (
        "an admitted repetition must cancel the pending silence nudge"
    )
    assert armed.cancelled() or armed.done()
    assert session._reprompt_count == 0

    session._cancel_reprompt()
    await _release(session, hold)


@pytest.mark.asyncio
async def test_admitted_exact_repeat_becomes_the_next_turn():
    """Admission is not just buffering: the words must reach a turn and history."""
    hold = asyncio.Event()
    session, loop, processed = make_session(hold=hold)
    await _dispatch_turn(session, loop)

    await session._accumulate_transcript(DATES)
    # The endpointing deadline expires INSIDE the active turn.
    loop.last.callback()
    await asyncio.sleep(0)
    assert processed == [DATES], "the queued repetition must not join the old turn"

    await _release(session, hold)
    _fire_live(loop)
    await asyncio.sleep(0)
    await asyncio.sleep(0)

    assert processed == [DATES, DATES], (
        "a repetition deferred by the active turn must dispatch once it releases"
    )
    assert session.full_transcript == [
        {"role": "user", "text": DATES},
        {"role": "user", "text": DATES},
    ]


@pytest.mark.asyncio
async def test_admitted_repeat_records_no_ignored_result_telemetry(caplog):
    """The event means "ignored". Admitted speech was not ignored."""
    hold = asyncio.Event()
    session, loop, _processed = make_session(hold=hold)
    await _dispatch_turn(session, loop)

    with caplog.at_level("INFO"):
        await session._accumulate_transcript(DATES)
        await session._set_transcript_interim("the fifth of september")

    assert _post_dispatch_records(caplog) == [], (
        "admitted speech must not be recorded as an ignored result"
    )

    await _release(session, hold)


@pytest.mark.asyncio
async def test_twilio_media_streams_admits_an_exact_repeat_too(caplog):
    """The narrowed policy is SHARED with the Twilio Media Streams path."""
    hold = asyncio.Event()
    session, loop, processed = make_session(hold=hold, smartpbx=False, lang="si")
    assert session._is_smartpbx_session() is False

    await _dispatch_turn(session, loop, "mata kamarayak one")

    with caplog.at_level("INFO"):
        await session._accumulate_transcript("mata kamarayak one")

    assert session._pending_transcript == "mata kamarayak one", (
        "a repetition on the Twilio transport is caller speech too"
    )
    assert _post_dispatch_records(caplog) == []
    assert "smartpbx_media" not in caplog.text

    await _release(session, hold)


# ---------------------------------------------------------------------------
# 2. elapsed time no longer discriminates — the boundary trio
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "elapsed_ms, label",
    [
        (1_999, "just under the retired two-second window"),
        (2_000, "exactly on the retired two-second window"),
        (2_001, "just over the retired two-second window"),
    ],
)
@pytest.mark.asyncio
async def test_exact_repeat_is_admitted_at_every_age(monkeypatch, elapsed_ms, label):
    """All three are admitted, which is the proof that age stopped deciding.

    `elapsed_ms` is pinned rather than slept for: a wall clock cannot be asked to
    land on an exact millisecond, and the point of the trio is that the three
    values are now indistinguishable to the policy.
    """
    hold = asyncio.Event()
    session, loop, processed = make_session(hold=hold)
    await _dispatch_turn(session, loop)

    monkeypatch.setattr(session, "_post_dispatch_elapsed_ms", lambda: elapsed_ms)
    await session._accumulate_transcript(DATES)

    assert session._pending_transcript == DATES, (
        f"an exact repetition {label} is still caller speech"
    )
    assert processed == [DATES]

    await _release(session, hold)


# ---------------------------------------------------------------------------
# 3. what remains provable without provider identity (preservation guards)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text", ["", "   ", "...", " . , ! ", "— …"],
)
@pytest.mark.asyncio
async def test_non_material_results_are_still_refused(text):
    """No alphanumeric content: there is no caller speech here to lose."""
    hold = asyncio.Event()
    session, loop, _processed = make_session(hold=hold)
    await _dispatch_turn(session, loop)

    timers = len(loop.scheduled)
    await session._accumulate_transcript(text)
    await session._set_transcript_interim(text)

    assert session._committed_transcript == ""
    assert session._pending_transcript == ""
    assert session._latest_interim == ""
    assert len(loop.scheduled) == timers, (
        "a non-material result must not arm an endpointing timer"
    )
    assert session._smartpbx_stt_final_events == 0, (
        "a refused result must be refused BEFORE the per-turn counters"
    )
    assert session._smartpbx_stt_interim_events == 0

    await _release(session, hold)


@pytest.mark.asyncio
async def test_non_material_refusal_still_emits_the_bounded_event(caplog):
    """The telemetry event survives, now covering only non-material refusals."""
    hold = asyncio.Event()
    session, loop, _processed = make_session(hold=hold)
    await _dispatch_turn(session, loop)

    with caplog.at_level("INFO"):
        await session._accumulate_transcript("   ")
        await session._set_transcript_interim("...")

    records = _post_dispatch_records(caplog)
    assert len(records) == 2, records
    assert [line.split("result_type=", 1)[1].split(" ", 1)[0] for line in records] == [
        "final", "interim",
    ]

    await _release(session, hold)
