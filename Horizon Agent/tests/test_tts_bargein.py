"""Regression test for the Sinhala "cut off in the middle" bug.

A turn is spoken sentence-by-sentence; each sentence is a separate TTS call
that sends its own ``tts_done`` mark, and Twilio echoes that mark only after it
finishes *playing* that sentence. Rime Arcana streams at ~realtime, so
sentence N's echoed mark (which clears ``_is_speaking``) arrives while sentence
N+1 is still streaming. The old in-stream guard ``if not self._is_speaking:
break`` then cut sentence N+1 off mid-audio — Sinhala truncation. ElevenLabs
sends its audio in one fast network burst so every sentence was buffered before
any mark echoed, which is why English/Arabic never showed it.

The fix keys in-stream interruption off ``_speak_generation`` (bumped only by a
real barge-in) via ``_tts_superseded``, not off ``_is_speaking``. This test
pins that contract.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

HORIZON = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HORIZON))

server = pytest.importorskip("server")  # needs fastapi/httpx (present in CI)


def _bare_session():
    """A MediaStreamSession with only the flags the guard reads — no WebSocket,
    no system-prompt build."""
    s = object.__new__(server.MediaStreamSession)
    s._is_speaking = True
    s._speak_generation = 0
    return s


def test_echoed_mark_does_not_supersede_current_tts():
    """An earlier sentence's echoed tts_done clears _is_speaking, but the
    sentence still streaming must NOT be treated as interrupted."""
    s = _bare_session()
    gen = s._speak_generation
    # Simulate Twilio echoing a prior sentence's tts_done mid-stream.
    s._is_speaking = False
    assert s._tts_superseded(gen) is False


def test_bargein_supersedes_current_tts():
    """A real barge-in bumps _speak_generation and must interrupt the stream."""
    s = _bare_session()
    gen = s._speak_generation
    s._speak_generation += 1  # what _handle_bargein does
    assert s._tts_superseded(gen) is True


def test_fresh_tts_not_superseded():
    s = _bare_session()
    gen = s._speak_generation
    assert s._tts_superseded(gen) is False
