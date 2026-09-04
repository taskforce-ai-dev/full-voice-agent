"""Direct SmartPBX Sinhala capture must understand natural spoken numbers.

Mirrors the seam pinned in `tests/test_capture_dictation_buffering.py` and
`tests/test_capture_spoken_override_paths.py`: the combined dictation reaches
`_process_utterance` (and, through `_last_guest_utterance_raw`, the
`capture_spoken_number` tool override) as ONE utterance. Normalisation is
restricted to a Direct SmartPBX Sinhala PHONE capture episode. Ordinary dates,
amounts, names and generic capture stay verbatim; a caller who says "හැට පහ
හතර තුන" while providing a phone number is captured just like an English
caller who dictates "six five four three".
"""

from __future__ import annotations

import asyncio

import pytest

import server


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


def make_smartpbx_session(*, lang: str):
    """A Direct SmartPBX session whose LLM turn is recorded instead of run."""
    session = server.MediaStreamSession(websocket=None, lang=lang, media_transport=None)
    session._smartpbx_transfer_context = object()
    session._media_transport = object()
    loop = FakeLoop()
    session._event_loop = loop
    processed: list[str] = []

    async def record(text):
        processed.append(text)
        # Mirror what production does at the end of every turn: the raw
        # (now-normalised) utterance is what the capture tool override sees.
        session._last_guest_utterance_raw = text

    session._process_utterance = record
    return session, loop, processed


def test_direct_smartpbx_is_asserted_by_the_fixture():
    session, _loop, _processed = make_smartpbx_session(lang="si")
    assert session._is_direct_smartpbx_sinhala() is True


@pytest.mark.asyncio
async def test_sinhala_capture_dictation_is_normalised_to_digits():
    session, loop, processed = make_smartpbx_session(lang="si")
    session._enter_capture_mode(kind="phone")

    await session._accumulate_transcript("හැට පහ හතර තුන")
    loop.last.callback()
    await asyncio.sleep(0)
    await asyncio.sleep(0)

    assert processed == ["65 4 3"], (
        "a Sinhala tens+units dictation must reach the turn as plain digits, "
        "not the untranslated Sinhala words"
    )
    # The same normalised text is what the capture tool override would see.
    assert session._last_guest_utterance_raw == "65 4 3"
    assert session._smartpbx_runner_raw_utterance() == "65 4 3"


@pytest.mark.asyncio
async def test_sinhala_capture_dictation_ratio_uses_the_normalised_digits():
    """The dictation-ratio exit check runs on the SAME normalised text, so a
    pure-Sinhala-number dictation is correctly recognised as a dictation."""
    session, loop, processed = make_smartpbx_session(lang="si")
    session._enter_capture_mode(kind="phone")

    await session._accumulate_transcript("හැට පහ")
    loop.last.callback()
    await asyncio.sleep(0)
    await asyncio.sleep(0)

    assert processed == ["65"]
    assert session._is_capture_mode_active() is True, (
        "a genuine Sinhala number dictation must not be mistaken for "
        "the caller changing the subject"
    )


@pytest.mark.asyncio
async def test_mixed_ascii_and_sinhala_digits_combine_correctly():
    session, loop, processed = make_smartpbx_session(lang="si")
    session._enter_capture_mode(kind="phone")

    for fragment in ("0 7 7", "හැට පහ"):
        await session._accumulate_transcript(fragment)
    loop.last.callback()
    await asyncio.sleep(0)
    await asyncio.sleep(0)

    assert processed == ["0 7 7 65"]


@pytest.mark.asyncio
async def test_english_direct_smartpbx_utterances_are_never_touched():
    """Guard: the Sinhala normaliser must not run on the English call profile."""
    session, loop, processed = make_smartpbx_session(lang="en")
    session._enter_capture_mode()

    await session._accumulate_transcript("zero seven seven")
    loop.last.callback()
    await asyncio.sleep(0)
    await asyncio.sleep(0)

    assert processed == ["zero seven seven"]


@pytest.mark.asyncio
async def test_delivered_sinhala_phone_ask_arms_phone_and_normalises_first_attempt():
    session, loop, processed = make_smartpbx_session(lang="si")
    ask = "කරුණාකර ඔබේ WhatsApp අංකය කියන්න."
    session._start_assistant_turn_delivery_tracking()
    generation = session._assistant_turn_generation
    session._record_generated_sentence(ask)
    session._record_delivered_sentence(ask, generation)
    session._maybe_enter_capture_mode_from_ask()

    assert session._capture_kind == "phone"
    await session._accumulate_transcript(
        "බිංදුව හත හත එක දෙක තුන හතර පහ හය හත"
    )
    assert loop.last.delay == server.CAPTURE_VALID_LK_NUMBER_GRACE_SECONDS
    loop.last.callback()
    await asyncio.sleep(0)
    await asyncio.sleep(0)

    assert processed == ["0 7 7 1 2 3 4 5 6 7"]


def test_twilio_sinhala_phone_ask_does_not_arm_direct_smartpbx_capture():
    session = server.MediaStreamSession(websocket=None, lang="si", media_transport=None)
    ask = "කරුණාකර ඔබේ WhatsApp අංකය කියන්න."
    session._start_assistant_turn_delivery_tracking()
    generation = session._assistant_turn_generation
    session._record_generated_sentence(ask)
    session._record_delivered_sentence(ask, generation)

    session._maybe_enter_capture_mode_from_ask()

    assert session._is_capture_mode_active() is False
    assert session._capture_kind == "generic"


@pytest.mark.asyncio
async def test_sinhala_generic_capture_with_number_words_stays_verbatim():
    session, loop, processed = make_smartpbx_session(lang="si")
    session._enter_capture_mode()

    await session._accumulate_transcript("ඔක්තෝබර් හැට පහ")
    loop.last.callback()
    await asyncio.sleep(0)
    await asyncio.sleep(0)

    assert processed == ["ඔක්තෝබර් හැට පහ"]


@pytest.mark.asyncio
async def test_sinhala_name_capture_with_number_words_stays_verbatim():
    session, loop, processed = make_smartpbx_session(lang="si")
    session._enter_capture_mode(kind="name")

    await session._accumulate_transcript("හැට පහ")
    loop.last.callback()
    await asyncio.sleep(0)
    await asyncio.sleep(0)

    assert processed == ["හැට පහ"]


@pytest.mark.asyncio
async def test_ordinary_sinhala_conversation_without_numbers_is_unchanged():
    session, loop, processed = make_smartpbx_session(lang="si")

    await session._accumulate_transcript("ඔබට කොහොමද")
    loop.last.callback()
    await asyncio.sleep(0)
    await asyncio.sleep(0)

    assert processed == ["ඔබට කොහොමද"]
