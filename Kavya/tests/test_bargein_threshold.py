"""Barge-in must require a substantive interruption, not a blip or echo.

While Kavya is speaking, ANY STT result used to clear her queued audio, so a
stray "mm-hmm", room noise, or her own TTS echoing into Azure STT swallowed the
rest of a sentence. A genuine sustained interruption must still barge in.
"""

from __future__ import annotations

import asyncio
import logging

import pytest

import server


def make_session(monkeypatch, lang="en", smartpbx=True):
    session = server.MediaStreamSession(websocket=None, lang=lang, media_transport=None)
    try:
        session._event_loop = asyncio.get_running_loop()
    except RuntimeError:
        session._event_loop = None
    if smartpbx:
        session._smartpbx_transfer_context = object()
    barged = []
    accumulated = []
    interims = []

    async def fake_bargein():
        barged.append(True)

    async def fake_accumulate(text):
        accumulated.append(text)

    async def fake_interim(text):
        interims.append(text)

    async def noop():
        return None

    if monkeypatch is not None:
        monkeypatch.setattr(session, "_handle_bargein", fake_bargein)
        monkeypatch.setattr(session, "_accumulate_transcript", fake_accumulate)
        monkeypatch.setattr(session, "_set_transcript_interim", fake_interim)
        monkeypatch.setattr(session, "_clear_media_audio", noop)
    return session, barged, accumulated, interims


def _seed_echo_session(session):
    session._start_assistant_turn_delivery_tracking()
    session._record_generated_sentence("yes, we can help with room bookings and half board options")
    session._record_generated_sentence("if you prefer, we can also include full board")
    generation = session._assistant_turn_generation
    session._record_delivered_sentence("yes, we can help with room bookings and half board options", generation)
    session._record_delivered_sentence("if you prefer, we can also include full board", generation)


def _seed_partial_echo_delivery_session(session):
    session._start_assistant_turn_delivery_tracking()
    session._record_generated_sentence("yes, we can help with room bookings and half board options")
    session._record_generated_sentence("if you prefer, we can also include full board")
    generation = session._assistant_turn_generation
    session._record_delivered_sentence("yes, we can help with room bookings and half board options", generation)


def test_should_barge_in_ignores_short_blips(monkeypatch):
    session, *_ = make_session(monkeypatch)
    session._speaking_since = 0.0  # outside any debounce window
    assert session._should_barge_in("mm") is False
    assert session._should_barge_in("yeah") is False
    assert session._should_barge_in("   ") is False
    assert session._should_barge_in("") is False


def test_should_barge_in_accepts_a_substantive_interruption(monkeypatch):
    session, *_ = make_session(monkeypatch)
    session._speaking_since = 0.0
    assert session._should_barge_in("no that is the wrong number") is True


def test_should_barge_in_debounces_the_start_of_speech(monkeypatch):
    session, *_ = make_session(monkeypatch)
    monkeypatch.setattr(server, "BARGEIN_DEBOUNCE_SECONDS", 0.5)
    # Kavya just started speaking: a transcript now is most likely her own echo.
    session._speaking_since = server.time.monotonic()
    assert session._should_barge_in("no that is the wrong number") is False
    # Well after the debounce window a real interruption gets through.
    session._speaking_since = server.time.monotonic() - 5.0
    assert session._should_barge_in("no that is the wrong number") is True


@pytest.mark.asyncio
async def test_interim_blip_while_speaking_does_not_barge_in(monkeypatch):
    session, barged, _accum, interims = make_session(monkeypatch)
    session._is_speaking = True
    session._speaking_since = 0.0
    session._on_stt_interim("mm-hmm")
    await asyncio.sleep(0)
    assert barged == [], "a short blip must not clear Kavya's audio"
    assert interims == [], "and it must not be treated as a new utterance while speaking"


@pytest.mark.asyncio
async def test_sustained_interim_while_speaking_barges_in(monkeypatch):
    session, barged, _accum, _interims = make_session(monkeypatch)
    session._is_speaking = True
    session._speaking_since = 0.0
    session._on_stt_interim("actually can you change my dates please")
    await asyncio.sleep(0.02)
    assert barged == [True], "a real sustained interruption must still barge in"


@pytest.mark.asyncio
async def test_final_blip_while_speaking_does_not_barge_in(monkeypatch):
    session, barged, _accum, _interims = make_session(monkeypatch)
    session._is_speaking = True
    session._speaking_since = 0.0
    session._on_stt_result("okay")
    await asyncio.sleep(0)
    assert barged == []


@pytest.mark.asyncio
async def test_normal_utterance_when_not_speaking_is_unchanged(monkeypatch):
    session, barged, accum, _interims = make_session(monkeypatch)
    session._is_speaking = False
    session._on_stt_result("I would like to book a room")
    await asyncio.sleep(0.02)
    assert barged == []
    assert accum == ["I would like to book a room"], "listening path must be untouched"


@pytest.mark.asyncio
async def test_echo_like_transcript_when_not_speaking_is_not_dropped(monkeypatch):
    session, barged, accum, _interims = make_session(monkeypatch)
    _seed_echo_session(session)
    session._is_speaking = False
    session._on_stt_result("yes we can help with room bookings and half board options")
    await asyncio.sleep(0.02)

    assert barged == []
    assert accum == ["yes we can help with room bookings and half board options"]


@pytest.mark.asyncio
async def test_twilio_media_streams_path_also_thresholds(monkeypatch):
    # Shared code: the Sinhala/Tamil Media Streams path uses the same guard.
    session, barged, _accum, _interims = make_session(monkeypatch, lang="si", smartpbx=False)
    session._is_speaking = True
    session._speaking_since = 0.0
    session._on_stt_interim("hmm")
    await asyncio.sleep(0)
    assert barged == []


def test_bargein_knob_defaults_and_clamping():
    assert server._parse_clamped_int({}, "X", 12, 0, 200) == 12
    assert server._parse_clamped_int({"X": "999"}, "X", 12, 0, 200) == 200
    assert server._parse_clamped_int({"X": "-5"}, "X", 12, 0, 200) == 0
    assert 0 <= server.BARGEIN_MIN_CHARS <= 200
    assert 0.0 <= server.BARGEIN_DEBOUNCE_SECONDS <= 5.0


def test_echo_normalizer_strips_punctuation_and_whitespace():
    assert server._normalize_for_overlap("Hello, ROOM!  Test... yes") == "hello room test yes"


def test_token_overlap_ratio_exact_echo_match():
    assert server._token_overlap_ratio(
        "yes, we can help with your booking",
        "Yes we can help with your booking",
    ) == 1.0


def test_token_overlap_ratio_handles_stt_noise():
    reference = "yes we can help with room bookings and half board options"
    transcript = "yes we can help with room booking and half board options please"
    assert server._token_overlap_ratio(reference, transcript) > 0.8


def test_token_overlap_ratio_partial_overlap_remains_genuine_interruption():
    reference = "i can help with half board"
    transcript = "yes half board please"
    assert server._token_overlap_ratio(reference, transcript) < server.ECHO_MATCH_MIN_RATIO


def test_not_resident_question_fragment_is_not_echo():
    session, *_ = make_session(None)
    _seed_echo_session(session)
    session._is_speaking = True
    assert (
        session._is_echo("I hear you. Are you a Sri Lankan resident or a foreign guest?")
        is False
    )


def test_spoken_yes_number_readback_is_not_echo():
    session, *_ = make_session(None)
    _seed_echo_session(session)
    session._is_speaking = True
    assert (
        session._is_echo("yes zero seven one one seven five four six eight")
        is False
    )


def test_full_sentence_echo_is_classified_as_echo():
    session, *_ = make_session(None)
    _seed_echo_session(session)
    session._is_speaking = True
    assert (
        session._is_echo("yes, we can help with room bookings and half board options")
        is True
    )


def test_exact_affirmation_match_with_leading_acknowledgment_is_still_echo():
    session, *_ = make_session(None)
    session._start_assistant_turn_delivery_tracking()
    sentence = (
        "yes, we can certainly help with room bookings and half board "
        "options for your stay"
    )
    session._record_generated_sentence(sentence)
    generation = session._assistant_turn_generation
    session._record_delivered_sentence(sentence, generation)
    session._is_speaking = True

    assert (
        session._is_echo("yes we can certainly help with room bookings and half "
                         "board options for your stay")
        is True
    )


def test_affirmation_prefixed_transcript_skips_non_affirmation_agent_sentence():
    session, *_ = make_session(None)
    session._start_assistant_turn_delivery_tracking()
    sentence = (
        "so that's zero seven one one, seven five four, six six eight. "
        "Is that right?"
    )
    session._record_generated_sentence(sentence)
    generation = session._assistant_turn_generation
    session._record_delivered_sentence(sentence, generation)
    session._is_speaking = True

    assert session._is_echo("yes zero seven one one seven five four six eight") is False


@pytest.mark.asyncio
async def test_short_transcript_below_threshold_is_not_echo(monkeypatch):
    session, _, accum, _ = make_session(monkeypatch)
    session._is_speaking = True
    _seed_echo_session(session)

    monkeypatch.setattr(server, "BARGEIN_MIN_CHARS", 12)
    monkeypatch.setattr(server, "ECHO_MATCH_MIN_RATIO", 0.8)
    session._on_stt_result("yes")
    await asyncio.sleep(0)

    assert accum == []


@pytest.mark.asyncio
async def test_echo_transcript_is_rejected_before_barge_in_and_not_added_to_transcript(caplog, monkeypatch):
    session, barged, accum, _ = make_session(monkeypatch)
    session._is_speaking = True
    session._speaking_since = server.time.monotonic() - 2.0
    _seed_echo_session(session)
    rejected = []

    def on_reject(chars: int, score: float) -> None:
        rejected.append((chars, score))

    session._record_echo_rejection = on_reject
    transcript = "yes we can help with room bookings and half board options"

    with caplog.at_level(logging.INFO):
        session._on_stt_result(transcript)
    await asyncio.sleep(0)

    assert barged == []
    assert accum == []
    assert rejected and rejected[0][0] == len(transcript)
    assert session._is_speaking is True
    assert transcript not in caplog.text
    assert "smartpbx_media event=echo_rejected" in caplog.text


@pytest.mark.asyncio
async def test_mid_turn_echo_of_second_sentence_is_rejected(monkeypatch):
    session, barged, accum, _ = make_session(monkeypatch)
    session._is_speaking = True
    _seed_partial_echo_delivery_session(session)
    session._speaking_since = server.time.monotonic() - 2.0

    session._on_stt_result("if you prefer, we can also include full board")
    await asyncio.sleep(0)

    assert barged == []
    assert accum == []


@pytest.mark.asyncio
async def test_genuine_smartpbx_interruption_after_debounce_still_barges_in(monkeypatch):
    session, barged, accum, _ = make_session(monkeypatch)
    session._is_speaking = True
    session._speaking_since = server.time.monotonic() - 2.0
    _seed_echo_session(session)
    rejection = []
    session._record_echo_rejection = lambda chars, score: rejection.append((chars, score))

    session._on_stt_result("yes please check my check-in dates")
    # run_coroutine_threadsafe lands the barge-in task on the NEXT loop turn
    await asyncio.sleep(0)
    await asyncio.sleep(0)

    assert barged == [True]
    assert accum == []
    assert rejection == []
