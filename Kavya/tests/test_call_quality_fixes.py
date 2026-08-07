"""Tests for three defects observed on live calls, 2026-07-31.

1. Tool-round text was concatenated without a separator, so the call log and the
   Google Sheet showed "…right away.I'm transferring…". Audio was fine — each
   round streams to Twilio separately — so this was only ever visible in the
   transcript, which is exactly where nobody notices it until a client reads it.

2. Post-call processing ran when the ConversationRelay session ended at TRANSFER
   time, before the call was actually over. That wrote a spurious "dropped" row,
   then a second correct row once the failsafe session finished — two rows for
   one call, the first one misleading. It must now be emitted exactly once, by
   whichever leg genuinely ends the call.

3. The failsafe session opened with Twilio's apology greeting and then waited in
   silence for the guest to speak — 13 s of dead air on the observed call, ended
   only by the guest saying "Okay" unprompted.
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import server  # noqa: E402


@pytest.fixture
def client():
    return TestClient(server.app)


@pytest.fixture(autouse=True)
def clean_handoff_state():
    server._handoff_state.clear()
    yield
    server._handoff_state.clear()


# ---------------------------------------------------------------------------
# 1. Transcript joining across tool rounds
# ---------------------------------------------------------------------------

def test_join_turn_inserts_separator_between_rounds():
    """The exact string observed on the live call."""
    got = server._join_turn(
        "Of course, let me transfer you right away.",
        "I'm transferring you now, Diva.",
    )
    assert got == "Of course, let me transfer you right away. I'm transferring you now, Diva."
    assert ".I'm" not in got


@pytest.mark.parametrize("acc,new,expected", [
    ("", "first", "first"),                    # no leading space
    ("acc", "", "acc"),                        # empty round is a no-op
    ("", "", ""),
    ("ends ", "next", "ends next"),            # don't double up existing space
    ("prev", " next", "prev next"),            # don't double up leading space
    ("prev", "\nnext", "prev\nnext"),          # newline already separates
    ("a.", "B.", "a. B."),
])
def test_join_turn_edges(acc, new, expected):
    assert server._join_turn(acc, new) == expected


def test_join_turn_never_adds_space_beyond_what_was_there():
    """The join must not manufacture whitespace at the seam.

    Whitespace already inside either side is the model's own and is preserved;
    what must never happen is the join itself creating a doubled space.
    """
    for acc in ["x", "x ", "x  "]:
        for new in ["y", " y", "  y"]:
            joined = server._join_turn(acc, new)
            # The seam contributes at most the whitespace the two sides brought.
            expected_gap = len(acc) - len(acc.rstrip(" ")) + len(new) - len(new.lstrip(" "))
            actual_gap = len(joined) - len(acc.rstrip(" ")) - len(new.lstrip(" "))
            assert actual_gap <= max(expected_gap, 1), (
                f"join manufactured whitespace: {acc!r} + {new!r} -> {joined!r}"
            )


# ---------------------------------------------------------------------------
# 2. Post-call emitted exactly once, by the leg that ends the call
# ---------------------------------------------------------------------------

def _seed_transfer(call_sid="CA_PC_1", turns=2):
    server._remember_handoff(
        call_sid,
        reason="guest asked for a human",
        caller_phone="+94776697566",
        transcript=[{"role": "user", "text": "hi"}] * turns,
        notified=False,
        call_start_time="2026-07-31T10:00:00",
        lang="en",
    )


def test_dial_result_answered_emits_post_call(client):
    """A successfully transferred call must still produce exactly one record.

    The relay session skips post-call at transfer time because it cannot yet
    know whether the human will pick up. If this branch did not emit, a
    successful transfer would leave NO row at all — a worse failure than the
    duplicate it replaced.
    """
    _seed_transfer("CA_PC_1")
    with patch.object(server, "process_post_call_data") as mock_pc:
        r = client.post(
            "/voice/dial-result",
            data={"DialCallStatus": "answered", "CallSid": "CA_PC_1"},
        )
    assert r.status_code == 200
    assert "<Hangup/>" in r.text
    assert mock_pc.call_count == 1, "answered transfer must emit exactly one record"
    kwargs = mock_pc.call_args.kwargs
    assert kwargs["call_sid"] == "CA_PC_1"
    assert kwargs["caller_phone"] == "+94776697566"
    assert kwargs["lang"] == "en"
    assert kwargs["call_start_time"] == "2026-07-31T10:00:00", \
        "must reuse the original start time, not the dial-result time"
    assert len(kwargs["full_transcript"]) == 2


def test_dial_result_answered_without_carryover_emits_nothing(client):
    """No stashed transcript (e.g. state already evicted) → nothing to write."""
    with patch.object(server, "process_post_call_data") as mock_pc:
        r = client.post(
            "/voice/dial-result",
            data={"DialCallStatus": "answered", "CallSid": "CA_UNKNOWN"},
        )
    assert r.status_code == 200
    assert mock_pc.call_count == 0


def test_dial_result_no_answer_does_not_emit_post_call(client):
    """The failsafe session is about to run and will emit the record itself.

    Emitting here too is precisely the duplicate-row bug this fix removes.
    """
    _seed_transfer("CA_PC_2")
    with patch.object(server, "process_post_call_data") as mock_pc:
        r = client.post(
            "/voice/dial-result",
            data={"DialCallStatus": "no-answer", "CallSid": "CA_PC_2"},
        )
    assert r.status_code == 200
    assert "handover_failsafe" in r.text, "should route into the failsafe session"
    assert mock_pc.call_count == 0


def test_dial_result_answered_clears_carryover(client):
    """Carry-over must not leak into a later call reusing the dict."""
    _seed_transfer("CA_PC_3")
    with patch.object(server, "process_post_call_data"):
        client.post(
            "/voice/dial-result",
            data={"DialCallStatus": "answered", "CallSid": "CA_PC_3"},
        )
    assert "CA_PC_3" not in server._handoff_state


def test_transfer_carryover_records_what_post_call_needs():
    """_remember_handoff must carry start time and lang forward.

    Without these the dial-result branch cannot reconstruct an accurate record
    and would silently substitute 'now' and 'en'.
    """
    _seed_transfer("CA_PC_4")
    state = server._handoff_state["CA_PC_4"]
    for key in ("transcript", "caller_phone", "call_start_time", "lang", "reason"):
        assert key in state, f"handoff carry-over missing {key!r}"


# ---------------------------------------------------------------------------
# 3. Failsafe opens the conversation instead of waiting
# ---------------------------------------------------------------------------

def test_failsafe_kickoff_exists_and_is_directive():
    k = server._FAILSAFE_KICKOFF
    assert k, "kickoff turn must exist or the guest hears silence"
    low = k.lower()
    assert "speak first" in low or "right now" in low
    # Must not make Kavya re-greet — Twilio already spoke the apology.
    assert "do not greet" in low or "don't greet" in low


def test_failsafe_kickoff_is_marked_as_system_not_guest_speech():
    """It is seeded into conversation_history but must never look like the guest.

    It is deliberately kept out of full_transcript; the marker makes that
    obvious if it ever leaks into a log.
    """
    assert server._FAILSAFE_KICKOFF.startswith("[SYSTEM:")


def test_failsafe_greeting_does_not_already_ask_for_the_name():
    """The greeting must stay generic.

    Hardcoding "may I have your name?" into the greeting would be wrong when the
    guest already gave their name on the pre-transfer leg — the prompt's STEP 1
    says to confirm it rather than ask again.
    """
    greeting = server.HANDOFF_FAILSAFE_GREETING.lower()
    assert "your name" not in greeting


# ---------------------------------------------------------------------------
# #121 — ConversationRelay STT hints so Kavya understands spoken digit
# shorthand ("double seven" -> 77) live, in the call.
# ---------------------------------------------------------------------------

def test_english_conversationrelay_carries_digit_shorthand_hints():
    """The English ConversationRelay TwiML must bias Google's STT toward the
    spoken digit shorthand ("double"/"triple" + digit words) so the transcriber
    emits those words and Kavya can expand them live. Without the hint,
    "double seven" is garbled and she never receives the word "double"."""
    twiml = server._build_conversation_relay_twiml(
        "host", "en", server.conversation_relay_config("en"))
    assert 'hints="' in twiml
    for token in ("double", "triple", "oh", "seven"):
        assert token in twiml, f"hint vocabulary missing {token!r}"


def test_transcription_language_is_off_by_default():
    """transcriptionLanguage is emitted ONLY when CR_TRANSCRIPTION_LANGUAGE is
    set (the en-IN A/B); the default must keep the unchanged en-US behaviour."""
    cfg = dict(server.conversation_relay_config("en"), transcription_language="")
    twiml = server._build_conversation_relay_twiml("host", "en", cfg)
    assert "transcriptionLanguage=" not in twiml
    cfg_in = dict(server.conversation_relay_config("en"), transcription_language="en-IN")
    assert 'transcriptionLanguage="en-IN"' in server._build_conversation_relay_twiml(
        "host", "en", cfg_in)



def test_normal_english_conversationrelay_uses_canonical_profile(client, monkeypatch):
    from english_voice_profile import load_kavya_english_voice_profile

    profile = load_kavya_english_voice_profile(
        {"KAVYA_EN_ELEVENLABS_VOICE_ID": "unit-test-canonical-voice"}
    )
    monkeypatch.setattr(server, "load_kavya_english_voice_profile", lambda: profile)
    incoming = client.post("/voice/incoming", headers={"host": "voice.example.test"})
    selected = client.post("/voice/language-selected", data={"Digits": "1"}, headers={"host": "voice.example.test"})
    for response in (incoming, selected):
        assert response.status_code == 200
        assert 'voice="unit-test-canonical-voice-flash_v2_5"' in response.text
