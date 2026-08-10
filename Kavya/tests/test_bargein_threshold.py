"""Barge-in must require a substantive interruption, not a blip or echo.

While Kavya is speaking, ANY STT result used to clear her queued audio, so a
stray "mm-hmm", room noise, or her own TTS echoing into Azure STT swallowed the
rest of a sentence. A genuine sustained interruption must still barge in.
"""

from __future__ import annotations

import asyncio

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

    monkeypatch.setattr(session, "_handle_bargein", fake_bargein)
    monkeypatch.setattr(session, "_accumulate_transcript", fake_accumulate)
    monkeypatch.setattr(session, "_set_transcript_interim", fake_interim)
    monkeypatch.setattr(session, "_clear_media_audio", noop)
    return session, barged, accumulated, interims


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
