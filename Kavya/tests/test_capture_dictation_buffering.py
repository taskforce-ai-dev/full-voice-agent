"""Dictated numbers and spellings must cost ONE turn, not one per fragment.

Production shape of the problem: a caller says a phone number in 2-4 digit
groups, the recognizer commits a final at every natural pause, and each final
used to dispatch a full LLM turn (capture tool -> needs_more -> re-ask). Ten
digits could burn four turns and still fail, and mid-number the silence
re-prompt would talk over the caller.

The fix has three moving parts, all pinned here at the seams a caller can hear:
  (a) capture mode is armed by the DELIVERED ask, before the caller answers,
  (b) while it is armed every provider final refreshes one combining window and
      only the combined utterance is dispatched, bounded and echo-filtered,
  (c) the episode is bounded — success, a non-dictation reply, or the turn cap
      all end it — and nothing buffered is lost when the call tears down.
"""

from __future__ import annotations

import asyncio

import pytest

import server
from smartpbx_protocol import CallContext, MediaFormat
from smartpbx_session import KavyaSmartPBXSession


class _Scheduled:
    def __init__(self, delay, callback):
        self.delay = delay
        self.callback = callback
        self.cancelled = False

    def cancel(self):
        self.cancelled = True


class FakeLoop:
    """Captures call_later so timer arming is asserted deterministically."""

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


def make_session(*, smartpbx=True, lang="en"):
    """A session whose LLM turn is recorded instead of run."""
    session = server.MediaStreamSession(websocket=None, lang=lang, media_transport=None)
    if smartpbx:
        session._smartpbx_transfer_context = object()
    loop = FakeLoop()
    session._event_loop = loop
    processed: list[str] = []

    async def record(text):
        processed.append(text)

    session._process_utterance = record
    return session, loop, processed


def _deliver(session, *sentences):
    """Speak and acknowledge `sentences` as one completed assistant turn."""
    session._start_assistant_turn_delivery_tracking()
    generation = session._assistant_turn_generation
    for sentence in sentences:
        session._record_generated_sentence(sentence)
    for sentence in sentences:
        session._record_delivered_sentence(sentence, generation)


# --- (a) the ask detector -------------------------------------------------

CAPTURE_ASKS = [
    "Could I take your phone number, please?",
    "May I have your mobile number?",
    "What's your WhatsApp number?",
    "Can you give me a callback number?",
    "Could you repeat the number for me?",
    "How do you spell that?",
    "Could you spell your surname for me?",
    "Please say it digit by digit.",
    "And the remaining digits?",
    "Could you give me the rest of the number?",
    # Verbatim re-ask phrasings the system prompt tells her to use.
    "Sorry, I was asking for your mobile number — could you say the digits please?",
    "Sorry, that came through as only eight digits — could you say your mobile "
    "number once more?",
    "Let's try once more, nice and slow — could you say your number one digit at "
    "a time?",
]

NOT_CAPTURE_ASKS = [
    "How many guests will be staying?",
    "We have the Forest Escape Suite available for those dates.",
    "The rate is seven hundred dollars per night, half board.",
    "Would you like me to check availability?",
    # Read-backs. These name a number but are confirmations, not asks: arming
    # capture mode here would keep a finished episode alive for three more turns.
    "So I have your phone number as oh seven seven, one two three.",
    "Your WhatsApp number is oh seven seven one two three four five six.",
    "That's correct, I have it noted.",
    "Let me confirm the number I have.",
    "Thank you, I have your mobile number.",
    # A count question, not a dictation.
    "Could you tell me the number of guests staying?",
    "May I have the number of nights you'd like?",
    # The prompt's read-back line, verbatim.
    "Let me read that back — zero seven seven, one two three, four five six "
    "seven, is that right?",
]


@pytest.mark.parametrize("sentence", CAPTURE_ASKS)
def test_ask_detector_recognises_a_real_capture_ask(sentence):
    assert server._is_capture_ask_sentence(sentence) is True


@pytest.mark.parametrize("sentence", NOT_CAPTURE_ASKS)
def test_ask_detector_ignores_ordinary_speech_and_readbacks(sentence):
    assert server._is_capture_ask_sentence(sentence) is False


def test_ask_detector_scans_every_delivered_sentence():
    assert server._detect_capture_ask(
        ["Lovely, I can hold that.", "May I have your mobile number?"]
    ) is True
    assert server._detect_capture_ask(["Lovely, I can hold that."]) is False
    assert server._detect_capture_ask([]) is False


def test_only_a_sentence_the_caller_HEARD_arms_capture_mode():
    session, _loop, _processed = make_session()

    # Generated but never acknowledged by TTS — a barge-in cut it off, so the
    # caller was never asked and must not be treated as dictating.
    session._start_assistant_turn_delivery_tracking()
    session._record_generated_sentence("May I have your mobile number?")
    session._maybe_enter_capture_mode_from_ask()
    assert session._is_capture_mode_active() is False

    _deliver(session, "May I have your mobile number?")
    session._maybe_enter_capture_mode_from_ask()
    assert session._is_capture_mode_active() is True
    assert session._capture_mode_turns_left == server.CAPTURE_MODE_MAX_TURNS


def test_the_ask_arms_capture_mode_at_the_end_of_the_turn():
    """The whole point: the FIRST fragment is already patient."""
    session, loop, _processed = make_session()

    async def scenario():
        async def fake_llm():
            _deliver(session, "Of course.", "Could I take your phone number?")
            return "Of course. Could I take your phone number?"

        session._run_llm_claude = fake_llm
        await session._process_utterance_bound("you can call me back")

    asyncio.run(scenario())

    assert session._is_capture_mode_active() is True

    asyncio.run(session._accumulate_transcript("zero seven seven"))
    assert loop.last.delay >= server.CAPTURE_ENDPOINTING_SILENCE_SECONDS


# --- (b) fragments combine into one dispatch ------------------------------

def test_finals_combine_into_a_single_dispatch_after_the_silence():
    session, loop, processed = make_session()
    session._enter_capture_mode()

    async def scenario():
        for fragment in ("zero seven seven", "one two three", "four five six eight"):
            await session._accumulate_transcript(fragment)
            # Every final refreshes ONE window instead of dispatching.
            assert processed == []
            assert loop.last.delay >= server.CAPTURE_ENDPOINTING_SILENCE_SECONDS
            assert len(loop.live) == 1

        loop.last.callback()
        await asyncio.sleep(0)
        await asyncio.sleep(0)

    asyncio.run(scenario())

    assert processed == ["zero seven seven one two three four five six eight"]
    assert session._pending_transcript == ""
    assert session._committed_transcript == ""


def test_interims_between_fragments_keep_the_committed_digits():
    session, loop, processed = make_session()
    session._enter_capture_mode()

    async def scenario():
        await session._accumulate_transcript("zero seven seven")
        await session._set_transcript_interim("one two")
        assert loop.last.delay == server.CAPTURE_ENDPOINTING_SILENCE_SECONDS
        await session._accumulate_transcript("one two three")
        loop.last.callback()
        await asyncio.sleep(0)
        await asyncio.sleep(0)

    asyncio.run(scenario())
    assert processed == ["zero seven seven one two three"]


def test_the_combined_capture_buffer_is_bounded():
    session, loop, processed = make_session()
    session._enter_capture_mode()

    async def scenario():
        for _ in range(40):
            await session._accumulate_transcript("zero seven seven one two three")
        assert len(session._committed_transcript) <= server.CAPTURE_BUFFER_MAX_CHARS
        # An interim on top of a full buffer stays bounded too.
        await session._set_transcript_interim("four five six")
        assert len(session._pending_transcript) <= server.CAPTURE_BUFFER_MAX_CHARS
        loop.last.callback()
        await asyncio.sleep(0)
        await asyncio.sleep(0)

    asyncio.run(scenario())

    assert len(processed) == 1
    assert 0 < len(processed[0]) <= server.CAPTURE_BUFFER_MAX_CHARS
    # The HEAD is kept: the digits said first are the start of the number.
    assert processed[0].startswith("zero seven seven")


def test_outside_capture_mode_a_final_still_endpoints_on_the_short_grace():
    session, loop, _processed = make_session()

    asyncio.run(session._accumulate_transcript("i would like a room"))

    assert loop.last.delay == server.STT_FINAL_GRACE_SECONDS


# --- (b2) the echo gate runs BEFORE the buffer ----------------------------

@pytest.mark.asyncio
async def test_an_echoed_readback_never_reaches_the_capture_buffer(monkeypatch):
    session, _loop, _processed = make_session()
    session._event_loop = asyncio.get_running_loop()
    session._enter_capture_mode()
    readback = "your whatsapp number is zero seven seven one two three four five six"
    _deliver(session, readback)
    session._is_speaking = True
    session._speaking_since = 0.0

    async def noop():
        return None

    monkeypatch.setattr(session, "_handle_bargein", noop)

    session._on_stt_result(readback)
    await asyncio.sleep(0.02)

    assert session._is_echo(readback) is True
    assert session._committed_transcript == ""
    assert session._pending_transcript == ""

    # A genuine fragment, once she has stopped speaking, is buffered.
    session._is_speaking = False
    session._on_stt_result("nine nine nine")
    await asyncio.sleep(0.02)
    assert session._committed_transcript == "nine nine nine"


# --- (b3) the silence re-prompt must not fire mid-number ------------------

@pytest.mark.asyncio
async def test_reprompt_is_suppressed_while_the_capture_buffer_is_pending(monkeypatch):
    session, loop, _processed = make_session()
    monkeypatch.setattr(server, "SILENCE_REPROMPT_DELAY", 0)
    spoken: list[str] = []

    async def fake_speak(text, *_args, **_kwargs):
        spoken.append(text)

    monkeypatch.setattr(session, "_invoke_speak", fake_speak)
    session._enter_capture_mode()
    await session._accumulate_transcript("zero seven seven")

    await session._reprompt_after_silence()

    assert spoken == [], "a nudge mid-number talks over the caller"
    assert session._reprompt_count == 0
    assert session._reprompt_task is not None, "the nudge must be re-armed, not dropped"
    session._cancel_reprompt()


@pytest.mark.asyncio
async def test_reprompt_is_suppressed_while_only_the_capture_timer_is_pending(monkeypatch):
    session, loop, _processed = make_session()
    monkeypatch.setattr(server, "SILENCE_REPROMPT_DELAY", 0)
    spoken: list[str] = []

    async def fake_speak(text, *_args, **_kwargs):
        spoken.append(text)

    monkeypatch.setattr(session, "_invoke_speak", fake_speak)
    session._enter_capture_mode()
    session._arm_endpointing(server.CAPTURE_ENDPOINTING_SILENCE_SECONDS)

    await session._reprompt_after_silence()

    assert spoken == []
    session._cancel_reprompt()


@pytest.mark.asyncio
async def test_reprompt_still_fires_on_a_genuinely_silent_caller(monkeypatch):
    session, _loop, _processed = make_session()
    monkeypatch.setattr(server, "SILENCE_REPROMPT_DELAY", 0)
    spoken: list[str] = []

    async def fake_speak(text, *_args, **_kwargs):
        spoken.append(text)

    monkeypatch.setattr(session, "_invoke_speak", fake_speak)

    await session._reprompt_after_silence()

    assert len(spoken) == 1
    assert session._reprompt_count == 1


# --- (c) exit conditions --------------------------------------------------

def test_a_successful_capture_ends_the_episode_and_its_readback_cannot_rearm_it():
    session, _loop, _processed = make_session()
    session._enter_capture_mode()

    session._capture_success_this_turn = False
    session._record_capture_tool_completion(
        "capture_spoken_number",
        {"status": "captured", "digits": "0771234568", "valid": True},
    )

    assert session._is_capture_mode_active() is False

    # The read-back sentence of that same turn must not re-arm capture mode.
    _deliver(session, "Thank you. Your phone number is oh seven seven, one two three.")
    session._maybe_enter_capture_mode_from_ask()
    assert session._is_capture_mode_active() is False


def test_needs_more_keeps_the_episode_but_never_refills_its_allowance():
    session, _loop, _processed = make_session()
    session._enter_capture_mode()
    session._capture_mode_turns_left = 1

    session._record_capture_tool_completion(
        "capture_spoken_number", {"status": "needs_more", "digits": "077"}
    )

    assert session._is_capture_mode_active() is True
    assert session._capture_mode_turns_left == 1, (
        "a re-ask must not hand an exhausted episode a fresh budget"
    )


def test_a_non_dictation_reply_exits_capture_mode():
    session, loop, processed = make_session()
    session._enter_capture_mode()

    async def scenario():
        await session._accumulate_transcript(
            "actually could you tell me what time breakfast is served"
        )
        loop.last.callback()
        await asyncio.sleep(0)
        await asyncio.sleep(0)

    asyncio.run(scenario())

    assert len(processed) == 1
    assert session._is_capture_mode_active() is False


def test_a_dictation_reply_stays_in_capture_mode():
    session, loop, processed = make_session()
    session._enter_capture_mode()

    async def scenario():
        await session._accumulate_transcript("zero seven seven one two three")
        loop.last.callback()
        await asyncio.sleep(0)
        await asyncio.sleep(0)

    asyncio.run(scenario())

    assert len(processed) == 1
    assert session._is_capture_mode_active() is True


def test_capture_knobs_are_env_tunable_and_clamped():
    """Both knobs follow the same contract as their endpointing neighbours."""
    parse_int = server._parse_clamped_int
    parse_float = server._parse_clamped_float

    assert parse_int({"CAPTURE_BUFFER_MAX_CHARS": "900"},
                     "CAPTURE_BUFFER_MAX_CHARS", 600, 60, 4000) == 900
    assert parse_int({}, "CAPTURE_BUFFER_MAX_CHARS", 600, 60, 4000) == 600
    assert parse_int({"CAPTURE_BUFFER_MAX_CHARS": "1"},
                     "CAPTURE_BUFFER_MAX_CHARS", 600, 60, 4000) == 60
    assert parse_int({"CAPTURE_BUFFER_MAX_CHARS": "999999"},
                     "CAPTURE_BUFFER_MAX_CHARS", 600, 60, 4000) == 4000
    assert parse_float({"CAPTURE_DICTATION_MIN_RATIO": "0.5"},
                       "CAPTURE_DICTATION_MIN_RATIO", 0.3, 0.0, 1.0) == 0.5
    assert parse_float({"CAPTURE_DICTATION_MIN_RATIO": "9"},
                       "CAPTURE_DICTATION_MIN_RATIO", 0.3, 0.0, 1.0) == 1.0
    assert parse_float({"CAPTURE_DICTATION_MIN_RATIO": "-2"},
                       "CAPTURE_DICTATION_MIN_RATIO", 0.3, 0.0, 1.0) == 0.0

    assert 60 <= server.CAPTURE_BUFFER_MAX_CHARS <= 4000
    assert 0.0 <= server.CAPTURE_DICTATION_MIN_RATIO <= 1.0


def test_the_dictation_ratio_threshold_is_read_live(monkeypatch):
    """An operator raising the bar must change behaviour without a redeploy."""
    session, loop, processed = make_session()
    session._enter_capture_mode()
    monkeypatch.setattr(server, "CAPTURE_DICTATION_MIN_RATIO", 0.99)

    async def scenario():
        # A dictation with one non-digit word: above 0.3, below 0.99.
        await session._accumulate_transcript("okay zero seven seven one two three")
        loop.last.callback()
        await asyncio.sleep(0)
        await asyncio.sleep(0)

    asyncio.run(scenario())

    assert len(processed) == 1
    assert session._is_capture_mode_active() is False


def test_dictation_ratio_separates_digits_from_conversation():
    assert server._capture_dictation_ratio("zero seven seven one two") == 1.0
    assert server._capture_dictation_ratio("0771234568") == 1.0
    assert server._capture_dictation_ratio("k a v y a") == 1.0
    assert (
        server._capture_dictation_ratio(
            "actually could you tell me what time breakfast is served"
        )
        < server.CAPTURE_DICTATION_MIN_RATIO
    )
    assert server._capture_dictation_ratio("") == 0.0


def test_a_capture_episode_is_bounded_by_the_turn_cap_despite_needs_more():
    """The allowance is spent AFTER the turn, so needs_more cannot reset it."""
    session, loop, processed = make_session()

    async def scenario():
        _deliver(session, "Could I take your phone number?")
        session._maybe_enter_capture_mode_from_ask()
        assert session._is_capture_mode_active() is True

        async def one_capture_turn(_text):
            # What every fragment turn actually does in production.
            session._record_capture_tool_completion(
                "capture_spoken_number", {"status": "needs_more", "digits": "077"}
            )

        session._process_utterance = one_capture_turn

        for _ in range(server.CAPTURE_MODE_MAX_TURNS):
            await session._accumulate_transcript("zero seven seven")
            loop.last.callback()
            await asyncio.sleep(0)
            await asyncio.sleep(0)

    asyncio.run(scenario())

    assert session._is_capture_mode_active() is False, (
        "a caller whose number never parses must not be asked forever"
    )


def test_the_arming_turn_does_not_itself_spend_the_allowance():
    session, loop, _processed = make_session()

    async def scenario():
        async def arm_capture(_text):
            # What _process_utterance_bound does at the end of every turn (see
            # test_the_ask_arms_capture_mode_at_the_end_of_the_turn).
            _deliver(session, "Could I take your phone number?")
            session._maybe_enter_capture_mode_from_ask()

        session._process_utterance = arm_capture
        await session._accumulate_transcript("you can reach me on my mobile")
        loop.last.callback()
        await asyncio.sleep(0)
        await asyncio.sleep(0)

    asyncio.run(scenario())

    assert session._is_capture_mode_active() is True
    assert session._capture_mode_turns_left == server.CAPTURE_MODE_MAX_TURNS


def test_capture_mode_survives_a_bargein_so_the_dictation_stays_patient():
    session, _loop, _processed = make_session()
    session._enter_capture_mode()

    async def noop(*_args, **_kwargs):
        return None

    session._clear_media_audio = noop
    asyncio.run(session._accumulate_transcript("zero seven seven"))
    asyncio.run(session._handle_bargein())

    assert session._is_capture_mode_active() is True
    assert session._pending_transcript == "", "the interrupted buffer no longer dispatches"
    # Barge-in ownership is SUPERSEDED for dispatch, RETAINED for the record:
    # the fragment does not become the next turn, but it is not lost either.
    assert session.full_transcript == [
        {"role": "user", "text": "zero seven seven"}
    ], "the superseded fragment must still reach the transcript"


# --- (c2) nothing buffered may be lost at teardown ------------------------

@pytest.mark.asyncio
async def test_transfer_teardown_forces_the_pending_capture_into_the_transcript():
    session, _loop, _processed = make_session()

    async def noop(*_args, **_kwargs):
        return None

    session._clear_media_audio = noop
    session._enter_capture_mode()
    await session._accumulate_transcript("zero seven seven one two three")

    await session.enter_transfer_pending()

    assert session.full_transcript == [
        {"role": "user", "text": "zero seven seven one two three"}
    ]
    assert session._pending_transcript == ""
    assert session.transfer_pending is True


@pytest.mark.asyncio
async def test_retention_covers_ordinary_speech_outside_capture_mode():
    """Flipped from `test_forced_dispatch_is_a_noop_outside_capture_mode`.

    The capture-mode gate was right when a half-dictated number was the only
    thing that could be left buffered at teardown. Since the post-dispatch
    predicate was narrowed, ordinary speech admitted while a turn held the
    dispatch guard is routinely pending too — and it is no less the guest's
    words, so it is retained on exactly the same terms.
    """
    session, _loop, _processed = make_session()
    session._pending_transcript = "ordinary speech mid-utterance"

    await session._retain_pending_speech("session_end")

    assert session.full_transcript == [
        {"role": "user", "text": "ordinary speech mid-utterance"}
    ]
    assert session._pending_transcript == ""


def _context() -> CallContext:
    return CallContext(
        call_id="dialog-media-leg",
        other_leg_call_id="dialog-safe-call",
        caller_id_number="+94000000000",
        callee_id_number="+94110000000",
        account_id="dialog-account",
        media_format=MediaFormat(encoding="g711_ulaw", sample_rate=8000),
    )


class _FakeTransport:
    async def send_audio(self, _audio):
        return None

    async def clear_audio(self):
        return None

    async def send_mark(self, _name):
        return None

    async def close(self):
        return None


@pytest.mark.asyncio
async def test_dialog_hangup_forces_the_pending_capture_into_the_post_call_record():
    session, _loop, _processed = make_session()
    session._enter_capture_mode()
    await session._accumulate_transcript("zero seven seven one two three four five six")

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

    assert seen == [
        [{"role": "user", "text": "zero seven seven one two three four five six"}]
    ], "a half-dictated number must survive the hangup"
