"""Endpointing latency for the shared Media Streams / SmartPBX call flow.

`MediaStreamSession` drives its own utterance endpointing off STT callbacks.
This is shared code: the Twilio Media Streams path (Sinhala/Tamil/Arabic) and
the Dialog SmartPBX path use the same methods. A provider FINAL (Azure
`recognized`) definitively segments the utterance, so it must flush after a
short grace instead of the long interim-fallback timer that costs a flat ~1.5s
on every turn. The long timer stays only for the interim-only fallback (Google
frequently never fires a final).
"""

from __future__ import annotations

import asyncio

import pytest

import server


TWILIO_MEDIA_STREAM_LANGS = ["si", "ta", "ar"]


class _Scheduled:
    def __init__(self, delay, callback):
        self.delay = delay
        self.callback = callback
        self.cancelled = False

    def cancel(self):
        self.cancelled = True


class FakeLoop:
    """Captures call_later durations so timer arming is asserted deterministically."""

    def __init__(self):
        self.scheduled: list[_Scheduled] = []

    def call_later(self, delay, callback):
        handle = _Scheduled(delay, callback)
        self.scheduled.append(handle)
        return handle

    @property
    def last(self) -> _Scheduled:
        return self.scheduled[-1]


def make_session(monkeypatch, lang="en"):
    session = server.MediaStreamSession(websocket=None, lang=lang, media_transport=None)
    loop = FakeLoop()
    session._event_loop = loop
    processed: list[str] = []

    async def record(text):
        processed.append(text)

    monkeypatch.setattr(session, "_process_utterance", record)
    return session, loop, processed


class _NoopMonkeyPatch:
    def setattr(self, *_args, **_kwargs) -> None:
        return None


# --- (a) provider final flushes after the short grace ---------------------

@pytest.mark.parametrize("lang", ["en", *TWILIO_MEDIA_STREAM_LANGS])
def test_provider_final_arms_the_short_grace_not_the_long_timer(monkeypatch, lang):
    session, loop, _processed = make_session(monkeypatch, lang)

    asyncio.run(session._accumulate_transcript("i'd like a room"))

    assert loop.last.delay == server.STT_FINAL_GRACE_SECONDS
    assert loop.last.delay < server.ENDPOINTING_SILENCE, (
        "a definitive provider final must not wait the full interim-fallback timer"
    )


# --- (b) interim-only keeps the long fallback timer -----------------------

@pytest.mark.parametrize("lang", ["en", *TWILIO_MEDIA_STREAM_LANGS])
def test_interim_arms_the_long_fallback_timer(monkeypatch, lang):
    session, loop, _processed = make_session(monkeypatch, lang)

    asyncio.run(session._set_transcript_interim("i'd like"))

    assert loop.last.delay == server.ENDPOINTING_SILENCE


@pytest.mark.parametrize("lang", TWILIO_MEDIA_STREAM_LANGS)
def test_twilio_interim_only_utterance_still_flushes_via_the_fallback(monkeypatch, lang):
    """Google (Twilio Sinhala/Tamil) often fires no final; the fallback endpoints."""
    session, loop, processed = make_session(monkeypatch, lang)

    async def scenario():
        await session._set_transcript_interim("mata kamarayak")
        assert loop.last.delay == server.ENDPOINTING_SILENCE
        # Fire the fallback timer's callback, as the real loop would.
        loop.last.callback()
        await asyncio.sleep(0)
        await asyncio.sleep(0)

    asyncio.run(scenario())
    assert processed == ["mata kamarayak"]


def test_capture_mode_timers_switch_and_revert():
    session, loop, _processed = make_session(_NoopMonkeyPatch())
    session._enter_capture_mode(2)

    # In capture mode a provider final no longer dispatches — it refreshes the
    # combining window — so it must never wait less than the capture-silence
    # timer. (It used to arm the short capture grace and dispatch, which is what
    # made every 2-4 digit fragment cost its own LLM turn.)
    asyncio.run(session._accumulate_transcript("071175"))
    assert loop.last.delay >= server.CAPTURE_ENDPOINTING_SILENCE_SECONDS
    assert loop.last.delay == max(
        server.CAPTURE_FINAL_GRACE_SECONDS, server.CAPTURE_ENDPOINTING_SILENCE_SECONDS
    )

    asyncio.run(session._set_transcript_interim("071175"))
    assert loop.last.delay == server.CAPTURE_ENDPOINTING_SILENCE_SECONDS

    session._exit_capture_mode()
    asyncio.run(session._accumulate_transcript("071175"))
    assert loop.last.delay == server.STT_FINAL_GRACE_SECONDS

    asyncio.run(session._set_transcript_interim("071175"))
    assert loop.last.delay == server.ENDPOINTING_SILENCE


def test_capture_mode_consumes_turns_and_auto_expires():
    session, _loop, _processed = make_session(_NoopMonkeyPatch())
    session._enter_capture_mode(2)

    session._consume_capture_mode_turn()
    assert session._is_capture_mode_active()
    session._consume_capture_mode_turn()
    assert not session._is_capture_mode_active()


# --- (c) the durations are env-driven and clamped -------------------------

def test_endpointing_seconds_parser_reads_clamps_and_defaults():
    parse = server._parse_endpointing_seconds
    assert parse({"X": "0.4"}, "X", 1.0, 0.05, 5.0) == 0.4
    assert parse({}, "X", 1.0, 0.05, 5.0) == 1.0
    assert parse({"X": "not-a-number"}, "X", 1.0, 0.05, 5.0) == 1.0
    assert parse({"X": ""}, "X", 1.0, 0.05, 5.0) == 1.0
    assert parse({"X": "99"}, "X", 1.0, 0.05, 5.0) == 5.0  # clamp high
    assert parse({"X": "0"}, "X", 1.0, 0.05, 5.0) == 0.05  # clamp low
    assert parse(
        {"CAPTURE_FINAL_GRACE_SECONDS": "10"},
        "CAPTURE_FINAL_GRACE_SECONDS",
        1.2,
        0.2,
        3.0,
    ) == 3.0
    assert parse(
        {"CAPTURE_ENDPOINTING_SILENCE_SECONDS": "0"},
        "CAPTURE_ENDPOINTING_SILENCE_SECONDS",
        1.5,
        0.5,
        5.0,
    ) == 0.5


def test_module_defaults_are_shorter_than_the_old_hardcoded_wait():
    assert server.STT_FINAL_GRACE_SECONDS < server.ENDPOINTING_SILENCE
    assert server.STT_FINAL_GRACE_SECONDS <= 0.6
    assert 0.05 <= server.STT_FINAL_GRACE_SECONDS
    assert server.ENDPOINTING_SILENCE <= 1.5


def test_methods_read_the_env_driven_values_live(monkeypatch):
    session, loop, _processed = make_session(monkeypatch)
    monkeypatch.setattr(server, "STT_FINAL_GRACE_SECONDS", 0.123)
    monkeypatch.setattr(server, "ENDPOINTING_SILENCE", 0.789)

    asyncio.run(session._accumulate_transcript("final"))
    assert loop.last.delay == 0.123

    asyncio.run(session._set_transcript_interim("interim"))
    assert loop.last.delay == 0.789


# --- (d) a continuation within the grace is captured, not cut off ---------

def test_continuation_after_a_final_within_grace_is_not_cut_off(monkeypatch):
    session, loop, processed = make_session(monkeypatch)

    async def scenario():
        await session._accumulate_transcript("i'd like a room")
        grace = loop.last
        assert grace.delay == server.STT_FINAL_GRACE_SECONDS

        # The caller keeps talking before the grace fires.
        await session._set_transcript_interim("i'd like a room for next weekend")

        assert grace.cancelled, "the grace timer must be cancelled so the caller is not cut off"
        assert loop.last is not grace
        assert loop.last.delay == server.ENDPOINTING_SILENCE
        return session._pending_transcript

    pending = asyncio.run(scenario())
    assert "i'd like a room" in pending, "the committed final must survive the continuation"
    assert "for next weekend" in pending
    assert processed == [], "nothing may be processed while the caller is still speaking"


def test_multi_segment_finals_accumulate_into_one_utterance(monkeypatch):
    session, loop, processed = make_session(monkeypatch)

    async def scenario():
        await session._accumulate_transcript("i'd like a room")
        await session._accumulate_transcript("for next weekend")
        loop.last.callback()
        await asyncio.sleep(0)
        await asyncio.sleep(0)

    asyncio.run(scenario())
    assert processed == ["i'd like a room for next weekend"]


def test_flush_clears_committed_and_pending_state(monkeypatch):
    session, loop, processed = make_session(monkeypatch)

    async def scenario():
        await session._accumulate_transcript("first utterance")
        loop.last.callback()
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        # A brand-new utterance must not carry the previous one's text.
        await session._set_transcript_interim("second")
        return session._pending_transcript

    pending = asyncio.run(scenario())
    assert processed == ["first utterance"]
    assert pending == "second"
    assert session._committed_transcript == ""


def test_smartpbx_flush_starts_a_turn_with_the_endpoint_source(monkeypatch):
    session, _loop, _processed = make_session(monkeypatch)
    session._smartpbx_transfer_context = object()
    session._media_transport = object()
    session._pending_transcript = "endpointed final"
    session._committed_transcript = "endpointed final"

    asyncio.run(session._flush_transcript())

    assert session._active_smartpbx_turn_id is not None
    assert session._turn_telemetry._turns[session._active_smartpbx_turn_id].endpoint_source == "final"
